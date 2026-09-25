# YouTube plugin

Tracks subscribers, total views and video count for your own channels, and
views and likes for individual videos you choose.

!!! success "Official, documented API"
    Uses the YouTube Data API v3 with your own free API key. No scraping.
    The one caveat is that the API rounds large subscriber counts; see
    [Exact vs. rounded](#exact-vs-rounded).

## What it collects

For each configured channel (`{key}` is a slug you choose; see below):

| Metric key | Kind | What the number is |
|---|---|---|
| `youtube.channel.{key}.subscribers` | gauge | Subscribers (rounded by YouTube on larger channels) |
| `youtube.channel.{key}.views` | cumulative | Total views across the channel |
| `youtube.channel.{key}.videos` | gauge | Public videos on the channel |

For each configured video:

| Metric key | Kind | What the number is |
|---|---|---|
| `youtube.video.{key}.views` | cumulative | Views of that video |
| `youtube.video.{key}.likes` | gauge | Likes on that video. Skipped for that poll if the uploader hides it. |

Channels and videos are independent: you can track videos without a
channel, a channel without videos, or both. Series are labelled with the
channel's or video's current title.

## Configuration

```yaml
plugins:
  youtube:
    enabled: true
    source: official
    channels:
      - key: main                          # your choice: a-z, 0-9, _ and -
        id: "UCxxxxxxxxxxxxxxxxxxxxxx"      # the channel ID, not the @handle
    # videos:
    #   - key: launch_video
    #     id: "dQw4w9WgXcQ"                 # from youtube.com/watch?v=<id>
    # max: 5
    # videos_max: 20
```

| Key | Required | Default | Meaning |
|---|---|---|---|
| `source` | yes | none | Must be `official`, the only source this plugin ships |
| `channels[].key` | yes, per channel | none | A permanent slug for the metric key, matching `[a-z0-9_-]+`. Pick it once: changing it starts a new series. |
| `channels[].id` | yes, per channel | none | The channel ID |
| `videos[].key` | yes, per video | none | A permanent slug, as for channels |
| `videos[].id` | yes, per video | none | The 11-character video ID |
| `max` | no | `5` | If more channels than this are configured, the poll fails |
| `videos_max` | no | `20` | If more videos than this are configured, the poll fails |

At least one channel or video must be configured.

**Why a `key` and not the ID?** Channel IDs are case-sensitive and mixed
case, which doesn't fit a metric key or a Home Assistant entity ID. The slug
you choose does, and it stays stable for the life of the series.

**Why `source` at all?** A series must never switch between an exact and a
rounded data source on its own. The history only stores changes, so a
switch would write a fake jump. The choice is explicit so it can only ever
change on purpose.

### Finding your channel ID

It's the 24-character string starting with `UC`, in YouTube Studio →
Settings → Channel → Advanced settings, or in a URL of the form
`youtube.com/channel/UC…`. A handle (`@yourname`) or custom URL isn't
accepted: resolving one costs an extra request and can be ambiguous, so
paste the ID itself.

## Credentials and requests

**An API key is required**, set in the **`NGU_YOUTUBE_API_KEY`**
environment variable, never in `config.yaml`. Create one in the Google Cloud
Console with the *YouTube Data API v3* enabled. It's free and needs no OAuth
consent: the key only reads public data.

- **Per poll:** one request per channel plus one per video.
- **Quota:** each request costs 1 unit of the free 10,000 units per day.
  Five channels and twenty videos at the 30-minute default use 1,200 a day.

## Exact vs. rounded

The API rounds `subscribers` to about three significant figures once a
channel is reasonably large. On a bigger channel, day-to-day changes show as
a flat line with an occasional step. Views and video counts are commonly
understood to be exact.

## When it fails

A poll is all-or-nothing: if any channel or video fails, the whole poll
fails. It fails when:

- `NGU_YOUTUBE_API_KEY` isn't set, `source` isn't `official`, or nothing is
  configured (no request is made)
- a channel or video ID isn't found
- a channel hides its subscriber count, or it reads exactly `0`. That
  looks like a broken reading, so the poll fails rather than store a false
  zero. A new video's `0` views is not guarded; that's legitimate.
- more channels or videos are configured than `max`/`videos_max`

Running out of daily quota comes back as a **`403`** with
`reason=quotaExceeded` in the error, and shows as `blocked`. The quota
resets daily, Pacific time.

See [When a plugin fails](index.md#when-a-plugin-fails) for how failures
show up in general.

## Stability

**Stable.** An official, versioned API. The API key is redacted from
every error the plugin raises, so it never lands in `/api/plugins` or the
logs.
