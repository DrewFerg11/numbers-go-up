# Writing a plugin

This page is the **doing**: a worked, end-to-end walkthrough that gets you
from "I want to track this counter" to a merged plugin, using the
**Abacus** plugin (`numbers_go_up/plugins/abacus.py`) as the running
example -- it's the simplest shipped plugin, and every snippet below is
quoted from the real file, not a simplified stand-in.

[`CONTRIBUTING.md`](https://github.com/DrewFerg11/numbers-go-up/blob/main/CONTRIBUTING.md)
and
[`_template.py`](https://github.com/DrewFerg11/numbers-go-up/blob/main/numbers_go_up/plugins/_template.py)
are the contract -- what a plugin must do. This page doesn't restate
either; it walks the sequence of actually writing one.

## 1. Pick a source

**Official, documented APIs first.** GitHub's REST API and YouTube's Data
API v3 are both official and documented -- that's most of why they're
the plugins with a `!!! success "Official, documented API"` admonition on
their own pages.

An **unofficial** endpoint (MakerWorld, TikTok) is acceptable only when:

- it's the site's own JSON endpoint, not a reverse-engineered private API
  behind auth you don't have
- it returns public data about the user's own account -- never another
  user's private data, and never something requiring a password or
  session cookie
- you document the risk on the plugin's own page (see MakerWorld's `!!!
  warning "Unofficial endpoint"`) so a break doesn't surprise anyone

Abacus (`https://abacus.jasoncameron.dev`) is official, documented, and
unauthenticated -- about as easy as a source gets.

## 2. Declare `METRICS`

Every key must start with `"<plugin-name>."` -- the plugin's name is its
filename stem, and this prefix is what the loader's contract check
enforces. Abacus has exactly one metric, and it's a **pattern key**
(one `{placeholder}`, occupying a whole dot-separated segment) because
the actual counters aren't known until config is read:

```python
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
```

Use a pattern key exactly when the subjects are only known at poll time --
a list of repos, models, or (here) counters read from config. Use fixed,
exact keys when the plugin always emits the same one thing per account
(MakerWorld's `makerworld.profile.likes` and friends). `kind`/`unit`/`icon`
are fixed **here**, at declare time, and can never be overridden per-poll
by `collect()`'s return -- only a per-subject `label`/`attrs` can be, which
is why Abacus's config lets each counter set its own `label` and `unit`
(carried through `attrs`, not `METRICS`) while `kind`/`icon` stay generic.

Get `kind` right: `cumulative` for a counter that only rises, `gauge` for
anything that can fall. Home Assistant's `state_class` comes straight from
this, and getting it wrong corrupts long-term statistics.

## 3. Implement `collect()`

The signature is fixed by the contract:

```python
def collect(config: dict, http) -> dict[str, int | float | dict]:
```

`config` is this plugin's own block from `config["plugins"]`. `http` is
the **shared client** from `numbers_go_up.http.build_client()` -- never
construct your own. Using the shared client is what gives you, for free:
an honest `User-Agent`, a default timeout, no cookie persistence across
requests, and `429`/`403` turned into `RateLimited`/`Blocked` exceptions
before your code ever sees them.

Validate config **before** making any request -- a plugin with no
identifier configured must fail without a network call, never fetch and
discard. Abacus validates every counter entry, deduplicates keys, and
checks the configured `max` up front in `_validate_counters()`; nothing
in that function touches `http`. Then it builds one request per counter
and maps the response into the return shape:

```python
def collect(config: dict, http) -> dict[str, int | float | dict]:
    counters = _validate_counters(config)
    base_url = _validate_base_url(config)

    result: dict[str, int | float | dict] = {}
    for counter in counters:
        key, namespace, name = counter["key"], counter["namespace"], counter["name"]
        body = _fetch_counter(http, base_url, namespace, name)
        value = _validate_value(body, namespace, name)
        # ... the regression guard against _last_known_values goes here --
        # see step 4 below ...
        metric_key = f"abacus.counter.{key}.value"
        result[metric_key] = {"value": value, "label": counter["label"], "attrs": ...}
    return result
```

(Trimmed for the walkthrough -- the real function also checks each
counter's value against `_last_known_values` before accepting it; see
step 4.) A value may be a plain number, or a dict `{"value": ...,
"label": ..., "attrs": {...}}` when you need a per-subject label or attrs
for a pattern key -- exactly what Abacus does, since each counter's
display label and unit are config, not `METRICS`.

**Per-poll metric metadata is not a thing.** There is no way to change a
key's `kind` or `unit` from inside `collect()` -- they come only from
`METRICS`, fixed once. If you find yourself wanting to set them per-poll,
the source doesn't fit the contract as-is; that's worth raising as an
issue, not working around.

**One request per configured subject**, never one request per poll for
everything -- a plugin tracking five counters makes five requests, each
subject to the plugin's own `max` cardinality guard (`_DEFAULT_MAX_COUNTERS
= 20` here, raised via config's `max` key).

## 4. Failure behaviour

Raise a plain exception (Abacus raises `ValueError` throughout) for
anything that means this poll failed: a validation error, a 404, a
malformed response. You do not need to catch anything yourself --
`scheduler.run_plugin_once` wraps the whole `collect()` call, starts and
finishes the `plugin_runs` row, and stores the tail of the traceback.
**A plugin must not try to handle scheduling, retries, or backoff itself**
-- that's the scheduler's job, not the plugin's.

`Blocked` (raised by the shared client on an HTTP 403) is different from
a generic error: it means the source is refusing this client outright --
not a blip, and the scheduler backs off instead of retrying next interval
regardless. You don't raise `Blocked` yourself; it comes from the shared
`http` client's response hook. Your own `collect()` code only needs to
let it propagate (don't catch-and-swallow a 403).

Abacus additionally guards against a **value regression** -- a counter
that expired and came back at 0, or a `get`/`hit` typo that inflated it --
with process-lifetime, module-level state (`_last_known_values`), the
same pattern as MakerWorld's `_warned_missing_model_ids`: a plain dict,
mutated across polls, reset only by a restart. This is a plugin-specific
concern, not part of the general contract; most plugins don't need it.

## 5. Capture a fixture and write the offline test

Commit a real captured response under `tests/fixtures/`, with any of your
own identifying values scrubbed. `tests/test_abacus_plugin.py` loads it
and drives `collect()` against an `httpx.MockTransport` -- no network,
which is what CI actually runs:

```python
def _client(handler) -> httpx.Client:
    return http.build_client(transport=httpx.MockTransport(handler))


def test_one_counter_makes_exactly_one_request(self):
    body = _fixture("abacus_info.json")
    ...
```

Then add the `@pytest.mark.live` test `ci-test.yml` excludes
(`pytest -q -m "not live"`) and `sources-canary.yml` runs daily:

```python
@pytest.mark.live
def test_live_abacus():
    namespace = os.environ.get("NGU_CANARY_ABACUS_NAMESPACE")
    key = os.environ.get("NGU_CANARY_ABACUS_KEY")
    if not namespace or not key:
        pytest.skip("NGU_CANARY_ABACUS_NAMESPACE/NGU_CANARY_ABACUS_KEY not set")

    client = http.build_client()
    try:
        result = abacus.collect(
            {
                "counters": [
                    {
                        "key": "canary",
                        "namespace": namespace,
                        "name": key,
                        "label": "Canary",
                        "unit": "hits",
                    }
                ]
            },
            client,
        )
    finally:
        client.close()

    assert "abacus.counter.canary.value" in result
```

**What a live test does when its secret is absent matters.** `pytest.skip()`
is correct *inside the test* -- a contributor running the suite locally
without your canary secret shouldn't see a failure. But the canary
workflow verifies the secret is set **before** it ever invokes `pytest`
(a dedicated step, failing the job with `::error::` if the secret is
missing or empty) -- precisely because a silent `pytest.skip()` exits 0,
and a canary that lost its secret would report green forever while
probing nothing. Don't rely on the test's own skip to catch that; the
workflow-level check is what actually catches it.

## 6. Opt-in config

A plugin is **inert until configured**. No default user ID, handle, repo,
or counter ships in the image, and a fresh install makes zero outbound
requests. Add your plugin's block to `config.yaml.example`, commented out
like every other plugin:

```yaml
#   abacus:
#     enabled: true              # every plugin is OFF until explicitly enabled
#     counters:
#       - key: my_counter           # metric-key slug: [a-z0-9_-]+, permanent
#         namespace: example.com
#         name: my-counter-name
#         label: My Counter
#         unit: hits
#     max: 20                    # cardinality guard on counters
```

`enabled: true` is required explicitly -- absence, not `enabled: false`,
is the off state every other plugin also uses.

## 7. Responsible Use

Every plugin must meet the numbered rules in `CONTRIBUTING.md`. The ones
that bite most often when writing a new one:

- **Use the shared `http` client only.** No plugin constructs its own.
- **One request per configured subject per poll**, capped by your own
  `max`. Not one request that returns everything, filtered client-side.
- **30-minute default interval, never below 5 minutes** (`plugins/__init__.py`'s
  `MIN_POLL_INTERVAL_SECONDS` floor, enforced regardless of what config
  requests).
- **Jitter only ever delays** a request, never brings one forward.
- **Platform-neutral naming** in metric keys and labels where the plugin
  itself isn't already platform-specific by nature (Abacus is generic on
  purpose -- see its module docstring's "Not Split-Flap-specific" section).
- **No credential in `config.yaml`.** Abacus needs none (unauthenticated),
  but a plugin that does (an API key, a token) reads it from an
  environment variable, the same way `github.py` reads `NGU_GITHUB_TOKEN`
  and `youtube.py` reads `NGU_YOUTUBE_API_KEY` -- never a config key.

## 8. Add it to the canary matrix

`sources-canary.yml`'s `probe` job is a matrix, one entry per plugin. Add
yours as one more `include:` item -- Abacus's is genuinely a one-line-per-
secret addition:

```yaml
- plugin: abacus
  secret_name: NGU_CANARY_ABACUS_NAMESPACE
  extra_secret_name: NGU_CANARY_ABACUS_KEY
```

`secret_name` is the one your live test's primary `os.environ.get(...)`
reads; add `extra_secret_name` only if your source needs a second secret
(an API key alongside an account identifier, same shape as YouTube's and
Abacus's own `extra_secret_name`). Then ask the maintainer to set the
actual secret values under repo Settings -> Secrets -- a contributor's PR
can't set secrets on someone else's repo.

## Checklist

Before opening the PR, this is what a reviewer actually checks -- use it
as your own self-review:

- [ ] `METRICS` keys all start with `"<plugin-name>."`, `kind` is honestly
      `cumulative` vs `gauge`, and any pattern key has exactly one
      `{placeholder}` occupying a whole segment
- [ ] `collect(config, http)` validates config before any request, uses
      the shared `http` client, and makes one request per configured
      subject
- [ ] Nothing in the plugin handles retries, backoff, or scheduling itself
- [ ] An offline fixture test exists under `tests/`, captured from a real
      response with identifying values scrubbed
- [ ] A `@pytest.mark.live` test exists, skips cleanly when its secret is
      absent, and is excluded by `ci-test.yml`'s `-m "not live"`
- [ ] The plugin is off by default: no identifier ships in the image, and
      `config.yaml.example` documents it commented out
- [ ] No credential lives in `config.yaml` -- only environment variables
- [ ] Added to `sources-canary.yml`'s matrix, with the maintainer informed
      of which secrets to set
- [ ] `ruff check` and `pytest -q -m "not live"` both pass
- [ ] This page and `CONTRIBUTING.md` don't contradict each other -- if
      they do, that's a bug to fix in the same PR, in whichever one is wrong
