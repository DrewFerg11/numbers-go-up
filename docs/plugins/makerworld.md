# MakerWorld plugin

Tracks the public counters on your own MakerWorld profile (downloads,
prints, likes, followers) and, if you opt in, the same counters for each of
your published models.

!!! warning "Unofficial endpoint"
    MakerWorld has no public API. This plugin reads the same unauthenticated
    JSON endpoint the site's own pages use, which can change without notice.
    When it does, the plugin fails its poll clearly rather than writing wrong
    numbers. See [When it fails](#when-it-fails).

## What it collects

See the [metric catalogue](metrics.md) for the full, generated key/kind/unit
table across every plugin -- the tables below add the "what the number
means" context it doesn't carry.

**Profile counters**, one request per poll:

| Metric key | Kind | What the number is |
|---|---|---|
| `makerworld.profile.design_downloads` | cumulative | Downloads of your designs |
| `makerworld.profile.design_prints` | cumulative | Prints of your designs |
| `makerworld.profile.instance_downloads` | cumulative | Downloads of your print profiles (MakerWorld's "instances") |
| `makerworld.profile.instance_prints` | cumulative | Prints of your print profiles |
| `makerworld.profile.likes` | gauge | Likes across your content |
| `makerworld.profile.collections` | gauge | Times your content was added to a collection |
| `makerworld.profile.followers` | gauge | Followers |
| `makerworld.profile.level` | gauge | Your creator level |

The download and print counts come from the profile's `MWCount` breakdown.
The response also has a top-level `downloadCount`, but it's inflated
relative to the real per-type counts, so this plugin deliberately ignores
it.

**Per-model counters**, only with `models.enabled: true`, for each
published model (`{id}` is the model's numeric ID):

| Metric key | Kind |
|---|---|
| `makerworld.model.{id}.downloads` | cumulative |
| `makerworld.model.{id}.prints` | cumulative |
| `makerworld.model.{id}.likes` | gauge |
| `makerworld.model.{id}.collections` | gauge |
| `makerworld.model.{id}.comments` | gauge |

Each model series is labelled with the model's current title, and links to
the model's page. A model you unpublish stops being returned, and its series
are retired automatically. Their history is kept.

## Configuration

Like every plugin, it's off until you set `enabled: true`, and it makes no
request until its identifiers are configured. See
[Opt-in and inert by default](index.md#opt-in-and-inert-by-default).

```yaml
plugins:
  makerworld:
    enabled: true
    user_id: "1234567890"      # your numeric MakerWorld user ID
    # poll_interval: 1800
    # models:
    #   enabled: true          # adds per-model counters
    #   include: []            # model IDs to track; empty = every published model
    #   max: 50                # safety cap, checked after include
```

| Key | Required | Default | Meaning |
|---|---|---|---|
| `user_id` | yes | none | Your MakerWorld user ID. Digits only: this is **not** your `@handle`. |
| `models.enabled` | no | `false` | Also track each published model |
| `models.include` | no | `[]` | Numeric model IDs to track. Empty tracks every published model. An ID that isn't in your published list is skipped, with one warning. |
| `models.max` | no | `50` | If more models than this are selected, the poll fails instead of creating that many series. Raise it deliberately if you have a big catalogue. |

## Credentials and requests

**No credentials.** It reads public data about your own account and never
asks for a password or cookie.

- **Profile only:** one request per poll.
- **With `models.enabled`:** plus one paged listing request per 100
  published models.

Requests go to MakerWorld's backend API host (`api.bambulab.com`), which
serves the same data as the website without the website's bot challenge.

## When it fails

The plugin fails the poll, and never writes a partial or zero value, when:

- `user_id` is missing or isn't numeric (no request is made at all)
- the response isn't JSON, which usually means a maintenance or challenge
  page
- an expected field is missing, which usually means the endpoint changed
  shape. Open a "Broken data source" issue.
- more models are selected than `models.max`

A **`403`** shows as `blocked` in `/api/plugins` and turns the dashboard dot
red immediately. MakerWorld's edge has refused some networks in the past,
so a 403 can depend on where you poll from. Before reporting one, check
whether it reproduces from another connection.

See [When a plugin fails](index.md#when-a-plugin-fails) for how failures
show up in general.

## Stability

**Unofficial, so expect occasional breakage.** The endpoint isn't
documented or promised by MakerWorld. The project runs a daily canary
against the live source to notice shape changes early, but a change on
MakerWorld's side can still break polling until the plugin is updated.
