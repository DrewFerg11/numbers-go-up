# Abacus plugin

Tracks counters on [Abacus](https://abacus.jasoncameron.dev), a public
counter API for static sites: any `namespace` and `name` pair you point it
at. It is not tied to any particular site. If a static page bumps an Abacus
counter (a "boards flashed" badge, a visitor count, a download button), this
plugin records how that number changes over time.

!!! success "Official, documented API"
    Uses Abacus's documented, unauthenticated HTTP API, and it does not
    need a browser-style User-Agent. That makes it the simplest source to
    keep working. Abacus is a small third-party service, though, so its
    availability is its operator's to keep up, not something this project
    controls.

## What it collects

See the [metric catalogue](metrics.md) for the full, generated key/kind/unit
table across every plugin -- the table below adds the "what the number
means" context it doesn't carry.

Each configured counter gets one series (`{key}` is the slug you choose in
config):

| Metric key | Kind | What the number is |
|---|---|---|
| `abacus.counter.{key}.value` | cumulative | The counter's current value. Abacus counters only rise in normal operation. |

Two details worth knowing:

- **One shared pattern, generic unit.** Every counter shares the single
  `abacus.counter.{key}.value` pattern, and the plugin contract fixes a
  pattern's `kind`, `unit` and icon once, when it is declared. The unit
  therefore stays a generic `count` on purpose: one namespace and name can
  count anything, and the plugin cannot know what. The `unit` you write in
  config (for example `flashes`) is not lost. It is carried on each series
  as the `unit` attribute, so it shows in the API and on the dashboard.
- **Both parts identify a counter.** The `namespace` alone does not identify
  a counter, and neither does the `name` alone. Both are required, and both
  must match exactly what the page you are tracking uses.

## Configuration

Like every plugin, it's off until you set `enabled: true`, and it makes no
request until its identifiers are configured. See
[Opt-in and inert by default](index.md#opt-in-and-inert-by-default).

```yaml
plugins:
  abacus:
    enabled: true
    counters:
      - key: boards_flashed          # your own permanent slug: [a-z0-9_-]+
        namespace: your-site.example # exactly as the page uses it
        name: flash-finished         # exactly as the page uses it
        label: Boards Flashed
        unit: flashes
    # max: 20
    # poll_interval: 1800
```

| Key | Required | Default | Meaning |
|---|---|---|---|
| `counters` | yes | none | A list of counters, each with the entry keys below |
| `counters[].key` | yes | none | Permanent slug for the metric key: lowercase letters, digits, `_` and `-`, and unique in the list. **Pick it once.** It cannot be renamed without losing history. |
| `counters[].namespace` | yes | none | Abacus namespace. No `/` and no whitespace. |
| `counters[].name` | yes | none | Abacus counter key within that namespace. No `/` and no whitespace. |
| `counters[].label` | yes | none | The name shown on the dashboard |
| `counters[].unit` | no | empty | Carried on the series as the `unit` attribute. It is **not** the series' unit of measurement (see above). |
| `max` | no | `20` | If more counters than this are configured, the poll fails instead of creating that many series |
| `base_url` | no | the public instance | Point at a self-hosted Abacus. Must be `https://` with a host, and no path, query or fragment. |

The `key` is separate from the real Abacus `namespace` and `name` because
those may contain characters (dots, mixed case) that a metric key cannot.

## Credentials and requests

**No credentials.** The source is unauthenticated and public, and there is
no environment variable to set.

- **Per poll:** one request per counter.
- **Read-only, by construction.** The plugin only ever requests Abacus's
  `/info/` route. It never builds a `/hit/` request, which would *increment*
  the counter, and a test asserts no request it can construct does. Tracking
  a counter never changes it.
- **Rate limit:** Abacus allows 30 requests per 10 seconds per IP, with a
  proper `429` and `Retry-After`, which the shared HTTP client honours.

## When it fails

A poll is all-or-nothing: if any counter fails, the whole poll fails rather
than some series updating and others silently going stale. It fails when:

- `counters` is missing or empty (no request is made), or an entry is
  malformed, has an invalid or duplicate `key`, or has a `namespace` or
  `name` containing `/` or whitespace
- more counters are configured than `max`
- Abacus answers `404`, or says `exists: false`. The counter was deleted,
  has **expired**, or the namespace or name is misspelled.
- the response is not JSON, has no numeric `value`, or the value is not
  finite
- a value comes back **lower** than the last one this process saw

Two of those deserve a longer note:

- **Counters expire after about six months without an *increment*.** Reads
  do not refresh that timer. Only a real `/hit/` from the page does. A
  counter that goes quiet for that long is garbage-collected upstream, and
  the next read is a `404`. The plugin treats that as a failed poll, never
  as a silent drop to 0 in a cumulative series.
- **The "value went backwards" check is best-effort.** The plugin remembers
  each counter's last value in memory for the life of the process. That
  catches a counter that expired and came back lower, or a repointed
  namespace, *within* a run of the service. It is not saved anywhere, so a
  restart forgets it, and a regression that lands right after a restart is
  not caught. Treat it as a helpful guard, not a guarantee.

A **`403`** shows as `blocked` in `/api/plugins` and turns the dashboard dot
red. See [When a plugin fails](index.md#when-a-plugin-fails) for how
failures show up in general.

## Known limitation

**This is a feel-good badge, not an audit trail.** The count is
client-side and unauthenticated: anyone who knows, or guesses, the
namespace and name can call Abacus's `/hit/` endpoint directly and inflate
it. Nothing about this plugin, or the source it reads, proves that the
number reflects real activity.

## Stability

**Stable, with the caveats above.** A documented API with a simple, fixed
response shape, so breakage from Abacus's side is unlikely. What can
happen is an expired counter or a service outage, and both show up as a
clearly failed poll.
