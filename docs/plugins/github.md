# GitHub plugin

Tracks stars, forks, watchers and open issues for your repositories, and
optionally the total download count of their release assets.

!!! success "Official, documented API"
    Uses GitHub's REST API, an official, documented API. The fields it reads
    are part of GitHub's published contract, so breakage from GitHub's side
    is unlikely.

## What it collects

See the [metric catalogue](metrics.md) for the full, generated key/kind/unit
table across every plugin -- the table below adds the "what the number
means" context it doesn't carry.

For each configured repo (`{id}` is the repo's permanent numeric ID):

| Metric key | Kind | What the number is |
|---|---|---|
| `github.repo.{id}.stars` | gauge | Stars |
| `github.repo.{id}.forks` | gauge | Forks |
| `github.repo.{id}.watchers` | gauge | Watchers: people subscribed to notifications |
| `github.repo.{id}.open_issues` | gauge | Open issues **and** open pull requests. GitHub counts them together, and splitting them would cost extra requests. |
| `github.repo.{id}.release_downloads` | cumulative | Downloads of every asset across every release. Only with `release_downloads: true`. |

Two details worth knowing:

- **Series follow renames.** Series are keyed by the repo's numeric ID, not
  `owner/name`, so renaming or transferring a repo keeps its history.
  GitHub redirects the old name and the plugin follows. The label updates
  to the new name on the next poll.
- **Watchers are real watchers.** GitHub's API has a `watchers_count` field
  that, confusingly, always equals the star count. This plugin reads
  `subscribers_count` instead, which is the number GitHub's UI shows as
  "Watching".

## Configuration

Like every plugin, it's off until you set `enabled: true`, and it makes no
request until its identifiers are configured. See
[Opt-in and inert by default](index.md#opt-in-and-inert-by-default).

```yaml
plugins:
  github:
    enabled: true
    repos: ["owner/name", "owner/other-repo"]
    # release_downloads: false
    # max: 20
    # poll_interval: 1800
```

| Key | Required | Default | Meaning |
|---|---|---|---|
| `repos` | yes | none | `"owner/name"` strings, exactly as in the repo's URL: `github.com/owner/name` |
| `release_downloads` | no | `false` | Also track total release-asset downloads per repo |
| `max` | no | `20` | If more repos than this are configured, the poll fails instead of creating that many series |

## Credentials and requests

**No credentials required.** Unauthenticated, GitHub allows 60 API
requests per hour from your IP, which easily covers a handful of repos at
the 30-minute default.

For more repos or a shorter interval, set a token in the
**`NGU_GITHUB_TOKEN`** environment variable, never in `config.yaml`. That
raises the limit to 5,000 requests per hour. A token with no scopes is
enough for public repos. The token is sent only to
`api.github.com`.

- **Per poll:** one request per repo.
- **With `release_downloads`:** plus one request per 100 releases, per repo.

## When it fails

A poll is all-or-nothing: if any repo fails, the whole poll fails, rather
than some series updating and others silently going stale. It fails when:

- `repos` is missing or empty (no request is made), or a repo isn't
  `owner/name`
- a repo doesn't exist or isn't visible (`404`)
- more repos are configured than `max`

Running out of rate limit usually comes back as a **`403`**, which shows as
`blocked` in `/api/plugins` and turns the dashboard dot red. Add a token,
poll less often, or track fewer repos.

See [When a plugin fails](index.md#when-a-plugin-fails) for how failures
show up in general.

## Known limitation

`release_downloads` is the **sum** across all current releases. Deleting a
release lowers it. Storage accepts the drop, and Home Assistant treats it
as a counter reset. That's expected, not a bug.

## Stability

**Stable.** An official, versioned API, so breakage from GitHub's side is
unlikely.
