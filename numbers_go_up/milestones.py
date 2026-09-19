"""Milestone webhook alerts: fire-once notifications when a counter crosses
a configured threshold ("Design Downloads passed 500").

This is the only module that reads a milestone webhook URL -- whichever
environment variable ``config["milestones"]["webhook_url_env"]`` names -- and
it never logs the URL or puts it in a stored error, only the host (see
``_webhook_host``). The URL itself is a credential, exactly like
``NGU_MQTT_PASSWORD`` in ``mqtt.py``: env var only, never config.yaml.

Follows the same shape as ``mqtt.Publisher``/``NoopPublisher``: the
scheduler calls one hook, ``on_poll_finished``, after every finished poll,
and when ``config["milestones"]`` is absent :func:`build_evaluator` returns
a :class:`NoopEvaluator` with the same interface so callers never need an
``if milestones:`` branch.

Fire-once semantics (see the issue for the full spec):

- A per-series marker (``state`` table, key ``milestone:{metric_key}``)
  holds the highest threshold ever fired for that series. A threshold at or
  below the marker never fires again -- a gauge dipping and recrossing it
  stays silent.
- A rule seen for the first time for a series (no marker yet -- a brand new
  series' first sample, or a rule just added to an existing one) never
  fires; the marker is initialised silently to the highest threshold at or
  below the current value.
- Delivery is at-least-once and bounded: the pending payload is recorded in
  ``state`` (key ``milestone_pending:{metric_key}``) *before* it's sent. A
  2xx response advances the marker and clears the pending row in one
  transaction. Any other outcome (non-2xx, timeout, connection error) keeps
  it pending for the next successful poll of that plugin. After 24h of
  failed retries it's dropped with one WARNING and the marker advances
  anyway -- a dead webhook can't cause a flood once it comes back.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from numbers_go_up import plugins, storage
from numbers_go_up.config import ConfigError
from numbers_go_up.plugins import LoadedPlugin

logger = logging.getLogger(__name__)

MARKER_PREFIX = "milestone:"
PENDING_PREFIX = "milestone_pending:"

# Failure Handling-style bound on retrying a stuck delivery (Failure
# Handling #4's "no backoff storms" cousin for milestones): a webhook that's
# been down for a full day is dropped rather than retried forever.
PENDING_MAX_AGE_SECONDS = 24 * 3600


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_number(value: float) -> str:
    """A number with thousands separators, for the human-readable
    ``message`` field only -- every other numeric field in the payload
    stays a plain JSON number."""
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    return f"{value:,}"


class Evaluator(Protocol):
    """The interface the scheduler calls after every finished poll."""

    def on_poll_finished(
        self,
        plugin_name: str,
        status: str,
        returned_keys: frozenset[str],
        previous_values: Mapping[str, float | None],
        current_values: Mapping[str, float],
    ) -> None: ...

    @property
    def status(self) -> dict[str, Any]: ...


class NoopEvaluator:
    """Milestones off. Every call is a no-op, so scheduler.py and main.py
    never need to branch on whether milestones are configured."""

    def on_poll_finished(
        self,
        plugin_name: str,
        status: str,
        returned_keys: frozenset[str],
        previous_values: Mapping[str, float | None],
        current_values: Mapping[str, float],
    ) -> None:
        pass

    @property
    def status(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "rules": [],
            "pending": [],
            "last_sent": None,
            "last_error": None,
        }


def _is_positive_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and value > 0


def validate_milestones_config(
    raw: Any, env: Mapping[str, str], loaded_plugins: list[LoadedPlugin]
) -> dict[str, Any] | None:
    """Validate and normalise ``config["milestones"]``.

    Returns ``None`` when the block is absent (milestones off). Raises
    :class:`ConfigError` for anything wrong with the block itself or its
    env var -- a bad *rule* fails startup here; a bad *delivery* (HA down,
    a wrong-but-well-formed URL) never does, that's :class:`MilestoneEvaluator`'s
    job at runtime.

    ``loaded_plugins`` is the enabled, contract-valid plugin list (as
    :func:`numbers_go_up.plugins.discover_plugins` returns) -- used only to
    check that a *pattern* rule matches some plugin's declared METRICS
    pattern (an exact key is allowed to not exist yet; a pattern with no
    matching template is almost certainly a typo).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(
            f"milestones config must be a mapping, got {type(raw).__name__}"
        )

    webhook_url_env = raw.get("webhook_url_env")
    if not isinstance(webhook_url_env, str) or not webhook_url_env.strip():
        raise ConfigError(
            "milestones.webhook_url_env is required and must be a non-empty "
            "string naming an environment variable"
        )

    webhook_url = env.get(webhook_url_env)
    if not webhook_url:
        raise ConfigError(
            f"milestones.webhook_url_env names {webhook_url_env!r}, but that "
            "environment variable is not set"
        )

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ConfigError("milestones.rules must be a non-empty list")

    known_patterns = {
        key
        for plugin in loaded_plugins
        for key in plugin.metrics
        if plugins.is_pattern_key(key)
    }

    rules: list[dict[str, Any]] = []
    for i, rule_raw in enumerate(rules_raw):
        if not isinstance(rule_raw, dict):
            raise ConfigError(f"milestones.rules[{i}] must be a mapping")

        metric = rule_raw.get("metric")
        if not isinstance(metric, str) or not metric.strip():
            raise ConfigError(
                f"milestones.rules[{i}].metric is required and must be a "
                "non-empty string"
            )

        if plugins.is_pattern_key(metric) and metric not in known_patterns:
            raise ConfigError(
                f"milestones.rules[{i}].metric {metric!r} is a pattern but matches "
                "no enabled plugin's declared METRICS pattern"
            )

        every = rule_raw.get("every")
        at = rule_raw.get("at")
        if every is None and at is None:
            raise ConfigError(
                f"milestones.rules[{i}] ({metric!r}) needs 'every' and/or 'at'"
            )

        if every is not None and not _is_positive_number(every):
            raise ConfigError(
                f"milestones.rules[{i}].every must be a positive number, got {every!r}"
            )

        at_values: list[float] = []
        if at is not None:
            if not isinstance(at, list) or not at:
                raise ConfigError(
                    f"milestones.rules[{i}].at must be a non-empty list of "
                    "positive numbers"
                )
            for value in at:
                if not _is_positive_number(value):
                    raise ConfigError(
                        f"milestones.rules[{i}].at must contain only positive "
                        f"numbers, got {value!r}"
                    )
                at_values.append(value)

        rules.append(
            {
                "metric": metric,
                "every": every,
                "at": sorted(set(at_values)),
            }
        )

    return {"webhook_url": webhook_url, "rules": rules}


def _highest_threshold_at_or_below(rule: dict[str, Any], value: float) -> float | None:
    """The highest configured threshold <= ``value``, or ``None`` if every
    threshold is above it. Used only to silently initialise a fresh marker
    -- never to decide whether to fire."""
    candidates: list[float] = []
    every = rule["every"]
    if every:
        candidate = math.floor(value / every) * every
        if candidate > 0:
            candidates.append(candidate)
    candidates.extend(t for t in rule["at"] if t <= value)
    return max(candidates) if candidates else None


def _highest_crossed_threshold(
    rule: dict[str, Any], previous: float, current: float, marker: float
) -> float | None:
    """The highest threshold T with ``previous < T <= current`` and
    ``T > marker``, or ``None`` if nothing new crossed.

    Only the highest matters: several thresholds crossed in one poll (a
    burst, or an ``every``+``at`` rule whose ranges overlap) still fire
    once, for the highest -- the marker then covers every lower one too.
    """
    candidates: list[float] = []
    every = rule["every"]
    if every:
        candidate = math.floor(current / every) * every
        if candidate > 0 and candidate > previous:
            candidates.append(candidate)
    candidates.extend(t for t in rule["at"] if previous < t <= current)
    candidates = [c for c in candidates if c > marker]
    return max(candidates) if candidates else None


class MilestoneEvaluator:
    """Evaluates milestone rules after every finished poll and delivers
    fire-once webhook notifications.

    ``http_client`` is the shared client the scheduler already threads
    through every plugin's ``collect()`` (see ``http.build_client``) --
    this module builds no client of its own.
    """

    def __init__(
        self,
        rules: list[dict[str, Any]],
        webhook_url: str,
        db_path: str | Path,
        http_client: httpx.Client,
    ) -> None:
        self._rules = rules
        self._webhook_url = webhook_url
        self._webhook_host = httpx.URL(webhook_url).host
        self._db_path = db_path
        self._http = http_client

        self._lock = threading.Lock()
        self._last_sent: float | None = None
        self._last_error: str | None = None

    # -- rule matching ---------------------------------------------------

    def _match_rule(self, metric_key: str) -> dict[str, Any] | None:
        """The first configured rule whose ``metric`` matches ``metric_key``,
        exact or pattern -- reusing ``plugins.resolve_metric`` rather than
        re-implementing pattern matching."""
        for rule in self._rules:
            if plugins.resolve_metric(metric_key, {rule["metric"]: {}}) is not None:
                return rule
        return None

    # -- webhook delivery --------------------------------------------------

    def _send(self, payload: dict[str, Any]) -> bool:
        """POST ``payload``. Returns whether it was accepted (2xx).

        Every failure mode -- non-2xx, timeout, connection error, and the
        shared client's own 429/403 exceptions -- is caught here: a bad
        webhook must never raise out of the evaluator. Only the host is
        ever logged or stored, never the URL.
        """
        try:
            response = self._http.post(self._webhook_url, json=payload)
        except Exception as exc:
            # Never interpolate str(exc): httpx.HTTPStatusError subclasses
            # (e.g. http.Blocked from a 403) embed the full request URL --
            # the webhook URL is a credential -- in their message. Only the
            # exception's type name is ever logged or stored.
            detail = type(exc).__name__
            with self._lock:
                self._last_error = f"delivery to {self._webhook_host} failed: {detail}"
            logger.warning(
                "Milestone webhook delivery to %s failed: %s",
                self._webhook_host,
                detail,
            )
            return False

        if 200 <= response.status_code < 300:
            with self._lock:
                self._last_sent = time.time()
                self._last_error = None
            return True

        with self._lock:
            self._last_error = (
                f"delivery to {self._webhook_host} failed: HTTP {response.status_code}"
            )
        logger.warning(
            "Milestone webhook delivery to %s failed: HTTP %s",
            self._webhook_host,
            response.status_code,
        )
        return False

    def _marker_at_least(self, metric_key: str, candidate: float) -> str:
        """The encoded marker value for a marker-advancing write: never
        lower than the currently stored marker.

        A pending record can be displaced by a newer, higher crossing
        while its own delivery is still failing (see the overwrite in
        ``_evaluate_key``); when that displaced record is later retried
        (from ``_retry_pending_for_plugin``) and finally succeeds, or ages
        out after 24h, its own ``_attempt_delivery`` call must not stamp
        the marker down to its own, now-stale threshold -- a later poll
        may have already advanced the marker past it. Every marker write
        in this class goes through this method so the marker can only
        ever move forward.
        """
        current_raw = storage.get_state(self._db_path, f"{MARKER_PREFIX}{metric_key}")
        current = _decode_threshold(current_raw) if current_raw is not None else 0.0
        return _encode_threshold(max(current, candidate))

    def _attempt_delivery(self, metric_key: str, record: dict[str, Any]) -> None:
        """Send one pending record; on success advance the marker and clear
        it (one transaction); on failure, drop it after 24h with one
        WARNING (advancing the marker anyway), otherwise leave it pending
        for the next successful poll of this series' plugin."""
        threshold = record["threshold"]
        if self._send(record["payload"]):
            storage.set_state_and_delete(
                self._db_path,
                sets={
                    f"{MARKER_PREFIX}{metric_key}": self._marker_at_least(
                        metric_key, threshold
                    )
                },
                delete_keys=[f"{PENDING_PREFIX}{metric_key}"],
            )
            return

        age = time.time() - record["queued_at"]
        if age >= PENDING_MAX_AGE_SECONDS:
            logger.warning(
                "Milestone %s: dropping pending delivery to %s after %s of failed "
                "retries; advancing the marker anyway",
                metric_key,
                self._webhook_host,
                f"{age / 3600:.1f}h",
            )
            storage.set_state_and_delete(
                self._db_path,
                sets={
                    f"{MARKER_PREFIX}{metric_key}": self._marker_at_least(
                        metric_key, threshold
                    )
                },
                delete_keys=[f"{PENDING_PREFIX}{metric_key}"],
            )

    # -- evaluation ----------------------------------------------------

    def _evaluate_key(
        self,
        metric_key: str,
        rule: dict[str, Any],
        previous: float | None,
        current: float,
        now: float,
        attempted: set[str],
    ) -> None:
        marker_raw = storage.get_state(self._db_path, f"{MARKER_PREFIX}{metric_key}")
        if marker_raw is None:
            # No marker yet: a brand new series, or a rule just added to an
            # existing one. Never fires -- initialise silently.
            initial = _highest_threshold_at_or_below(rule, current)
            storage.set_state(
                self._db_path,
                f"{MARKER_PREFIX}{metric_key}",
                _encode_threshold(initial if initial is not None else 0),
            )
            return

        marker = _decode_threshold(marker_raw)
        if previous is None:
            # First sample of a new series never fires, even if a marker
            # somehow already exists (shouldn't happen, but never guess).
            return

        threshold = _highest_crossed_threshold(rule, previous, current, marker)
        if threshold is None:
            return

        series = storage.get_series_by_key(self._db_path, metric_key)
        label = (
            series["label"] if series is not None and series["label"] else metric_key
        )
        unit = series["unit"] if series is not None else None
        url = None
        if series is not None and series["attrs"]:
            try:
                url = json.loads(series["attrs"]).get("url")
            except (json.JSONDecodeError, AttributeError):
                url = None

        payload = {
            "message": f"{label} passed {_format_number(threshold)} "
            f"(now {_format_number(current)})",
            "metric": metric_key,
            "label": label,
            "unit": unit,
            "threshold": threshold,
            "value": current,
            "previous": previous,
            "url": url,
            "timestamp": _iso(now),
        }
        record = {"threshold": threshold, "payload": payload, "queued_at": now}
        # Recorded before sending, and a newer (higher) crossing overwrites
        # any older, still-undelivered pending record for the same series
        # -- the highest threshold always wins, and the retry timer
        # restarts for it (it's a different notification, not a retry of
        # the old one). The fire-once invariant lives in the marker, not
        # in whichever pending record happens to survive longest: the
        # marker is stamped to at least the higher of the two thresholds
        # right now, in the same transaction as the overwrite -- covering
        # both the discarded record (so a later dip-and-recross of it
        # stays silent) and this new one (so a later regression at
        # delivery time, via ``_marker_at_least``, can't uncover it
        # either, even if this record itself is later displaced again and
        # its eventual delivery/24h-drop would otherwise stamp a stale,
        # lower threshold).
        sets = {f"{PENDING_PREFIX}{metric_key}": json.dumps(record)}
        existing_raw = storage.get_state(self._db_path, f"{PENDING_PREFIX}{metric_key}")
        if existing_raw is not None:
            try:
                existing_threshold = json.loads(existing_raw).get("threshold")
            except json.JSONDecodeError:
                existing_threshold = None
            if isinstance(existing_threshold, int | float):
                sets[f"{MARKER_PREFIX}{metric_key}"] = self._marker_at_least(
                    metric_key, max(existing_threshold, threshold)
                )
        storage.set_state_and_delete(self._db_path, sets=sets, delete_keys=[])
        self._attempt_delivery(metric_key, record)
        attempted.add(metric_key)

    def _retry_pending_for_plugin(
        self, plugin_name: str, now: float, skip: set[str]
    ) -> None:
        """Retry every still-pending delivery for ``plugin_name``'s series,
        except ones this same poll already attempted via a fresh crossing
        (``_evaluate_key``) -- so a just-queued, just-failed delivery isn't
        immediately re-POSTed a second time in the same poll; it waits for
        the plugin's *next* successful poll, per the spec.
        """
        prefix = f"{PENDING_PREFIX}{plugin_name}."
        for state_key, raw in storage.get_state_prefix(self._db_path, prefix).items():
            metric_key = state_key[len(PENDING_PREFIX) :]
            if metric_key in skip:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "Milestone %s: corrupt pending record; dropping", metric_key
                )
                storage.delete_state(self._db_path, state_key)
                continue
            try:
                self._attempt_delivery(metric_key, record)
            except Exception:
                logger.exception(
                    "Milestone %s: error retrying pending delivery", metric_key
                )

    def _handle_poll_finished(
        self,
        plugin_name: str,
        returned_keys: frozenset[str],
        previous_values: Mapping[str, float | None],
        current_values: Mapping[str, float],
        now: float,
    ) -> None:
        attempted: set[str] = set()
        for metric_key in returned_keys:
            rule = self._match_rule(metric_key)
            if rule is None:
                continue
            current = current_values.get(metric_key)
            if current is None:
                continue
            try:
                self._evaluate_key(
                    metric_key,
                    rule,
                    previous_values.get(metric_key),
                    current,
                    now,
                    attempted,
                )
            except Exception:
                # Isolation: one series' bad data/exception never blocks
                # another series' evaluation or delivery.
                logger.exception("Milestone %s: error evaluating", metric_key)

        self._retry_pending_for_plugin(plugin_name, now, attempted)

    def on_poll_finished(
        self,
        plugin_name: str,
        status: str,
        returned_keys: frozenset[str],
        previous_values: Mapping[str, float | None],
        current_values: Mapping[str, float],
    ) -> None:
        """Called by the scheduler after every finished poll. Never
        raises -- a milestone error must never fail a plugin run."""
        if status != "ok":
            return
        try:
            self._handle_poll_finished(
                plugin_name, returned_keys, previous_values, current_values, time.time()
            )
        except Exception:
            logger.exception(
                "Milestone evaluator: error handling poll finish for %s", plugin_name
            )

    @property
    def status(self) -> dict[str, Any]:
        pending = []
        for state_key, raw in storage.get_state_prefix(
            self._db_path, PENDING_PREFIX
        ).items():
            metric_key = state_key[len(PENDING_PREFIX) :]
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            pending.append(
                {
                    "metric": metric_key,
                    "threshold": record.get("threshold"),
                    "since": record.get("queued_at"),
                }
            )
        pending.sort(key=lambda p: p["metric"])

        with self._lock:
            last_sent = self._last_sent
            last_error = self._last_error

        return {
            "enabled": True,
            "rules": [
                {"metric": rule["metric"], "every": rule["every"], "at": rule["at"]}
                for rule in self._rules
            ],
            "pending": pending,
            "last_sent": last_sent,
            "last_error": last_error,
        }


def _encode_threshold(value: float) -> str:
    return repr(float(value))


def _decode_threshold(raw: str) -> float:
    return float(raw)


def build_evaluator(
    config: dict[str, Any],
    loaded_plugins: list[LoadedPlugin],
    http_client: httpx.Client,
    env: Mapping[str, str] | None = None,
) -> Evaluator:
    """Build the evaluator ``main.py`` and the scheduler use.

    Returns a :class:`NoopEvaluator` when ``config["milestones"]`` is
    absent. Raises :class:`ConfigError` for a present but invalid block
    (including the env var naming a webhook URL that isn't set) -- never
    for an unreachable webhook, which is only ever discovered later, on
    delivery.
    """
    env = os.environ if env is None else env
    milestones_config = validate_milestones_config(
        config.get("milestones"), env, loaded_plugins
    )
    if milestones_config is None:
        return NoopEvaluator()

    return MilestoneEvaluator(
        milestones_config["rules"],
        milestones_config["webhook_url"],
        config["storage"]["path"],
        http_client,
    )
