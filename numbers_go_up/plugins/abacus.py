"""Abacus plugin: generic static-site counters via the public Abacus API
(https://abacus.jasoncameron.dev), read-only.

**Not Split-Flap-specific.** The originating use case (#96) is the "boards
flashed" badge on <https://drewferg11.github.io/Split-Flap-Display/...>,
which increments an Abacus counter client-side whenever ESP Web Tools
finishes an install. Namespace/key/label are entirely config-driven --
nothing Split-Flap ships in the image.

Source properties (probed Sep 19 2026, see #96): official, documented,
unauthenticated GET; no browser UA needed; 30 requests/10s per IP with
proper 429 + Retry-After (handled by the existing http.py path). No
Responsible Use #3 exception required.

**``/info/``, never ``/get/`` or ``/hit/``.** ``/info/:ns/:key`` is the one
URL template this module ever builds (:data:`_INFO_URL_TEMPLATE`) -- it
returns ``exists``/``value``/``expires_in``/``is_genuine`` where ``/get/``
returns only a bare ``{"value": N}``, and ``exists`` is load-bearing for the
expiry case below. ``/hit/:ns/:key`` **increments the counter** -- a
one-character slip between "get"/"info" and "hit" would silently inflate
the tracked count forever, with no admin key on record to undo it (Abacus
shows the admin key once, at creation). ``collect()`` never builds a
request path beyond namespace and key, both percent-encoded and validated
to reject ``/`` and whitespace up front, and
``tests/test_abacus_plugin.py`` asserts no request path this plugin can
construct ever contains ``/hit``.

Two real caveats, not solved here, just surfaced:

1. **Client-side and unauthenticated.** Anyone can hit ``/hit/`` directly
   and inflate the number. This is a feel-good badge, not an audit trail --
   said again in the README next to the metric.
2. **~6-month expiry from the last *increment* (not read).** A counter with
   no fresh hits in that window is garbage-collected and a future read
   starts back at zero/404 -- handled below as a failed poll, never a
   silent drop to 0 in a ``cumulative`` series.

**In-memory-only regression guard (deliberate, scoped trade-off).**
``collect(config, http)`` has no access to storage -- confirmed by reading
scheduler.py's ``run_plugin_once`` (storage happens only after ``collect()``
returns) and every other plugin's signature. There is also no generic
regression-rejection in storage.py: ``record_sample`` is store-on-change
only. So the issue's "a value lower than the last stored sample fails the
poll" requirement is implemented here as process-lifetime, module-level
state (:data:`_last_known_values`), the same precedent as MakerWorld's
``_warned_missing_model_ids`` in makerworld.py: a plain dict, mutated
across polls, never reset except by a process restart. This is
**best-effort, not a substitute for real persistence** -- it resets to
empty on every restart, so a regression across a restart is not caught.
Within a running process, though, it does catch exactly what #96 is aiming
at: a typo that swaps ``get``/``hit`` inflating a counter mid-run, or a
counter that expired and came back at a lower/zero value. Documented again
at the state dict's definition and in the README.

**``unit`` and the plugin contract.** ``abacus.counter.{key}.value`` is one
``METRICS`` pattern shared by every configured counter -- like every other
pattern-keyed plugin here (``github.repo.{id}.stars``,
``makerworld.model.{id}.downloads``), its ``kind``/``unit``/``icon`` are
fixed once, at declare time, and can never vary per poll
(``scheduler.run_plugin_once`` rejects a per-poll dict that sets
``"unit"``). So while config accepts a per-counter ``unit`` (e.g.
``flashes``), it cannot become that series' actual
``unit_of_measurement`` -- only ``label``/``attrs`` can vary per counter.
Rather than silently discard the configured value, it's carried through
into ``attrs["unit"]`` so it's still visible via the API/dashboard, and
this file's ``METRICS`` unit stays generic (``"count"``). This is a
deliberate, documented deviation from the issue's metrics table (which
lists unit as "from config") forced by the plugin contract, not an
oversight -- see the README's Abacus section.
"""

import json
import logging
import re
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1800  # 30 min

METRICS = {
    "abacus.counter.{key}.value": {
        "kind": "cumulative",  # Abacus counters only rise in normal operation
        "label": "Abacus Counter",
        # Fixed and generic, not per-counter: see the module docstring's
        # "unit and the plugin contract" section -- config's per-counter
        # `unit` rides in attrs["unit"] instead.
        "unit": "count",
        "icon": "mdi:counter",
    },
}

_DEFAULT_MAX_COUNTERS = 20

# Metric-key slug: Home-Assistant-entity-id-safe, permanent once picked --
# same convention as youtube.py's channel `key` (#62/#95 precedent),
# because Abacus namespaces are hostnames (dots) and names may contain
# characters outside this charset.
_KEY_PATTERN = re.compile(r"^[a-z0-9_-]+$")

# namespace/name: non-empty, no '/' (would split the URL path into extra
# segments -- the one way a config value could otherwise reshape the
# request path) and no whitespace (a copy-paste artifact, never intentional
# here). Encoded with urllib.parse.quote(safe="") regardless, so even a
# character this pattern allows through can't be interpreted as a path
# separator once encoded.
_BAD_NAMESPACE_CHARS = re.compile(r"[/\s]")

# The one URL template this module ever builds a request from -- `/info/`,
# never `/get/` or `/hit/`. See the module docstring.
_INFO_URL_TEMPLATE = "{base_url}/info/{namespace}/{key}"

# Process-lifetime, deliberately never reset except by a restart -- same
# convention as makerworld.py's `_warned_missing_model_ids`. Keyed by the
# config `key` slug (guaranteed unique by `_validate_counters`), not by
# (namespace, name): a `key` is documented as permanent, while a
# repointed namespace/name for the same `key` is exactly the case this
# guard exists to catch. Updated only after a value passes the check
# below, per-counter, independent of whether the rest of this poll
# succeeds -- see the module docstring.
_last_known_values: dict[str, int | float] = {}


def _validate_max_counters(value: object) -> int:
    """Same convention as github.py's ``_validate_max_repos``: a plain int
    (bools are int subclasses but not counts), at least 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"max must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"max must be at least 1, got {value!r}")
    return value


def _validate_base_url(config: dict) -> str:
    base_url = config.get("base_url", "https://abacus.jasoncameron.dev")
    if not isinstance(base_url, str):
        raise ValueError(f"base_url must be a string, got {base_url!r}")
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise ValueError(f"base_url must be https://, got {base_url!r}")
    if not parsed.netloc:
        raise ValueError(f"base_url must have a host, got {base_url!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"base_url must have no query or fragment, got {base_url!r}")
    # Normalize away a trailing slash so `_INFO_URL_TEMPLATE` never doubles
    # one up into `//info/...`.
    return base_url.rstrip("/")


def _validate_ns_or_name(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string, got {value!r}")
    if _BAD_NAMESPACE_CHARS.search(value):
        raise ValueError(f"{what} must not contain '/' or whitespace, got {value!r}")
    return value


def _validate_counters(config: dict) -> list[dict]:
    """Validate ``counters`` up front, before any request is made.

    Returns the validated list of {"key", "namespace", "name", "label",
    "unit"} dicts, order preserved. Raises ValueError on a missing/empty/
    non-list ``counters``, a malformed entry, an invalid or duplicate
    ``key``, a bad ``namespace``/``name``, or exceeding ``max``.
    """
    counters = config.get("counters")
    if not counters:
        raise ValueError("counters is not configured")
    if not isinstance(counters, list):
        raise ValueError(f"counters must be a list, got {type(counters).__name__}")

    max_counters = _validate_max_counters(config.get("max", _DEFAULT_MAX_COUNTERS))

    validated: list[dict] = []
    seen_keys: set[str] = set()
    for entry in counters:
        if not isinstance(entry, dict):
            raise ValueError(f"counters entry must be a mapping, got {entry!r}")

        key = entry.get("key")
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            raise ValueError(f"counters entry key {key!r} must match [a-z0-9_-]+")
        if key in seen_keys:
            raise ValueError(f"counters key {key!r} is a duplicate")
        seen_keys.add(key)

        namespace = _validate_ns_or_name(
            entry.get("namespace"), f"counters[{key!r}].namespace"
        )
        name = _validate_ns_or_name(entry.get("name"), f"counters[{key!r}].name")

        label = entry.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError(
                f"counters[{key!r}].label must be a non-empty string, got {label!r}"
            )

        unit = entry.get("unit", "")
        if not isinstance(unit, str):
            raise ValueError(f"counters[{key!r}].unit must be a string, got {unit!r}")

        validated.append(
            {
                "key": key,
                "namespace": namespace,
                "name": name,
                "label": label,
                "unit": unit,
            }
        )

    if len(validated) > max_counters:
        raise ValueError(
            f"counters count ({len(validated)}) exceeds max ({max_counters})"
        )
    return validated


def _fetch_counter(http, base_url: str, namespace: str, name: str) -> dict:
    url = _INFO_URL_TEMPLATE.format(
        base_url=base_url,
        namespace=quote(namespace, safe=""),
        key=quote(name, safe=""),
    )
    # Belt-and-suspenders on top of the input validation above and the
    # fixed template itself: every request this module ever issues targets
    # the "/info/" route, never "/hit/" -- see the module docstring and the
    # dedicated test. A positive check on the route prefix, not a
    # substring-ban on "hit" (a namespace or name is allowed to legitimately
    # *contain* "hit", e.g. a counter literally named "hit-counter" --
    # what must never happen is the *route* becoming "/hit/..."). A plain
    # check (not `assert`, which `python -O` strips) so this guard can
    # never silently disappear.
    if not httpx.URL(url).path.startswith("/info/"):
        raise ValueError(f"refusing to build a request outside of /info/: {url!r}")

    response = http.get(url, timeout=15)
    if response.status_code == 404:
        raise ValueError(
            f"Abacus counter {namespace}/{name} not found (404) -- deleted, "
            "expired (~6 months since the last increment), or a typo'd "
            "namespace/name"
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"Abacus counter {namespace}/{name} request failed: {exc}"
        ) from exc

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Abacus counter {namespace}/{name} response is not valid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ValueError(
            f"Abacus counter {namespace}/{name} response is not a JSON object"
        )

    if body.get("exists") is False:
        raise ValueError(
            f"Abacus counter {namespace}/{name} does not exist (exists: false) "
            "-- deleted, expired, or a typo'd namespace/name"
        )
    return body


def _validate_value(body: dict, namespace: str, name: str) -> int | float:
    if "value" not in body or body["value"] is None:
        raise ValueError(
            f"Abacus counter {namespace}/{name} response is missing 'value'"
        )
    value = body["value"]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(
            f"Abacus counter {namespace}/{name} 'value' must be numeric, got {value!r}"
        )
    return value


def collect(config: dict, http) -> dict[str, int | float | dict]:
    """config["counters"]: list of {"key", "namespace", "name", "label",
    "unit"?}. No default -- the plugin makes no request at all until it's
    set.

    config["base_url"]: override for a self-hosted Abacus instance, must
    be https:// with no query/fragment. Defaults to the public instance.

    config["max"]: cardinality guard on counters, default 20.

    Exactly one GET per counter, to `/info/`, never `/get/` or `/hit/`.
    Any counter failing (404/exists:false, missing/non-numeric value,
    unparseable JSON, or a value below the last one this process saw)
    fails the whole poll -- counters come from config, not discovery, so
    nothing is ever deactivated here.
    """
    counters = _validate_counters(config)
    base_url = _validate_base_url(config)

    result: dict[str, int | float | dict] = {}
    for counter in counters:
        key = counter["key"]
        namespace = counter["namespace"]
        name = counter["name"]

        body = _fetch_counter(http, base_url, namespace, name)
        value = _validate_value(body, namespace, name)

        last = _last_known_values.get(key)
        if last is not None and value < last:
            raise ValueError(
                f"Abacus counter {key} ({namespace}/{name}) value {value} is "
                f"lower than the last stored sample {last} -- refusing "
                "(counter reset, expired and recreated, or namespace/name "
                "repointed?). Note: this check is process-memory only and "
                "resets on restart, see the module docstring"
            )
        # Update immediately, per counter, independent of whether a later
        # counter in this same poll fails -- see the module docstring.
        _last_known_values[key] = value

        url = _INFO_URL_TEMPLATE.format(
            base_url=base_url,
            namespace=quote(namespace, safe=""),
            key=quote(name, safe=""),
        )
        attrs = {
            "namespace": namespace,
            "abacus_key": name,
            "url": url,
            "expires_in": body.get("expires_in"),
            "unit": counter["unit"],
        }
        result[f"abacus.counter.{key}.value"] = {
            "value": value,
            "label": counter["label"],
            "attrs": attrs,
        }

    return result
