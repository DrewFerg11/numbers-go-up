# Plugins

Each data source is a plugin: one Python file that the scheduler discovers
and polls on an interval. Pages for the built-in plugins:

| Plugin | Reads | Source |
|---|---|---|
| [MakerWorld](makerworld.md) | Profile counters, and optionally per-model counters, for your own account | Unofficial endpoint |
| [GitHub](github.md) | Stars, forks, watchers, open issues and release downloads for your repos | **Official**, documented API |
| [YouTube](youtube.md) | Subscribers, views and video count for your channels, and views/likes for individual videos | **Official**, documented API |

The image ships more built-in plugins than are documented here so far.
The full set is in
[`numbers_go_up/plugins/`](https://github.com/DrewFerg11/numbers-go-up/tree/main/numbers_go_up/plugins),
and each one is configured in the commented
[`config.yaml.example`](https://github.com/DrewFerg11/numbers-go-up/blob/main/config.yaml.example).

## Opt-in and inert by default

This rule applies to every plugin, built-in or your own:

- **Off until you enable it.** A plugin does nothing until its block in
  `config.yaml` sets `enabled: true`.
- **No request without an identifier.** An enabled plugin with no account,
  repo or channel configured fails its poll without making a request. It
  never fetches something and throws it away.
- **Nothing baked in.** No user IDs, handles or repos ship in the image, so
  a fresh install makes **zero outbound requests** to any platform.
- **Public data about your own accounts.** No plugin reads anyone's private
  data, and none asks for your password or session cookies.
- **Polite.** A 30-minute default interval that can't go below 5 minutes,
  an honest User-Agent (the TikTok plugin is the one documented
  exception: its pages only serve data to a browser-shaped request), real
  backoff when a source answers
  `429 Too Many Requests`, and as few requests per poll as the source
  allows. Each plugin page says exactly what it requests.

## Metric kinds

Every metric a plugin declares is one of two kinds, which decides how it's
stored and what Home Assistant does with it:

- **`cumulative`**: a running total that only goes up (downloads, views).
  Home Assistant gets `state_class: total_increasing`.
- **`gauge`**: a current level that can go down as well as up (followers,
  stars, likes). Home Assistant gets `state_class: measurement`.

Each plugin page lists the kind of every metric it emits.

## When a plugin fails

A failure is always **one failed poll, never a crash**. The service stays
up, other plugins keep running, and the failed plugin tries again on its
next scheduled poll.

**On the dashboard**, the status line shows one dot per enabled plugin.
Hover it to see the last error:

| Dot | Meaning |
|---|---|
| Green | The last poll succeeded, or a poll is running now |
| Amber | The last one or two polls failed |
| Red | Three or more polls in a row failed, **or** the source answered `403` |
| Grey | Enabled, but no poll has finished yet |

**In the API**, `GET /api/plugins` returns the same verdict as `health`
(`ok`, `warn`, `error`, or `pending`/`disabled`), alongside each plugin's
`status`:

| `status` | Meaning |
|---|---|
| `disabled` | Not enabled in `config.yaml` |
| `pending` | Enabled, but no poll has finished yet |
| `ok` | The last finished poll succeeded |
| `error` | The last finished poll failed. `last_error` says why, and `consecutive_failures` counts the streak. |
| `blocked` | The last finished poll got a `403`: the source is refusing this client. That's a different problem from a broken plugin, and it can depend on the network you're polling from. |
| `polling` | A poll is running right now |

To be alerted rather than check, point an uptime monitor at
`/health/plugins`, which returns `503` in exactly the cases that turn a dot
red.

## Writing your own

Want a source that isn't here? A plugin is one Python file in
`./user-plugins`. See [Writing a plugin](writing-a-plugin.md).
