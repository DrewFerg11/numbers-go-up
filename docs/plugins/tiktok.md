# TikTok plugin

Tracks followers, following and video count for your own TikTok handle(s),
and views and likes for individual videos you choose.

!!! warning "Unofficial source"
    TikTok has no public API for this data, so this plugin is **unofficial**.
    It reads a rendered web page, not an API: the same page data your
    browser receives when it opens a profile or video, which TikTok can
    change without notice. When that happens the plugin fails its poll
    clearly rather than writing wrong numbers. It can also be refused with a
    `403` from some networks. See [When it fails](#when-it-fails).

## What it collects

See the [metric catalogue](metrics.md) for the full, generated key/kind/unit
table across every plugin -- the tables below add the "what the number
means" context it doesn't carry.

**Account counters**, for each configured handle (`{key}` is the slug you
choose in config):

| Metric key | Kind | What the number is |
|---|---|---|
| `tiktok.user.{key}.followers` | gauge | Followers |
| `tiktok.user.{key}.following` | gauge | Accounts you follow |
| `tiktok.user.{key}.videos` | gauge | Videos on the account. It can fall when a video is deleted. |

**Video counters**, for each configured video (`{key}` is the slug you
choose in config):

| Metric key | Kind | What the number is |
|---|---|---|
| `tiktok.video.{key}.views` | cumulative | Views of that video |
| `tiktok.video.{key}.likes` | gauge | Likes on that video. It can fall when someone un-likes. |

!!! note "Account-level likes are deliberately absent"
    TikTok's total-likes-received counter for a whole profile (`heartCount`)
    is not collected, and it never appears in this plugin's metrics. It
    overflows a signed 32-bit integer for large accounts, and was seen as a
    **negative number** in a live capture. Recording it would put garbage in
    your history. A single video's like count does not have this problem and
    is tracked normally. This is a decision, not an oversight.

Two more details:

- **Large numbers are rounded by the page.** Above roughly a million,
  TikTok rounds the figures it shows, and this plugin reads them as shown.
  On a big account the day-to-day change can look like a flat line with an
  occasional step. That is TikTok's rounding, not a bug here.
- **Series follow your `key`, not the handle.** A handle can contain dots or
  capital letters that a metric key cannot, so you choose the `key` once and
  keep it.

## Configuration

Like every plugin, it's off until you set `enabled: true`, and it makes no
request until its identifiers are configured. See
[Opt-in and inert by default](index.md#opt-in-and-inert-by-default).

Handles and videos are independent. Configure either, or both.

```yaml
plugins:
  tiktok:
    enabled: true
    handles:
      - key: main              # your own permanent slug: [a-z0-9_-]+
        handle: yourhandle     # the part after @ in your profile URL
        # allow_zero_followers: true
    # max: 5
    videos:                    # optional
      - key: launch_video      # your own permanent slug: [a-z0-9_-]+
        id: "7123456789012345678"   # the number in the video's URL
    # videos_max: 20
    # poll_interval: 1800
```

| Key | Required | Default | Meaning |
|---|---|---|---|
| `handles` | one of `handles` or `videos` | none | A list of accounts to track |
| `handles[].key` | yes | none | Permanent slug for the metric key: lowercase letters, digits, `_` and `-`, and unique in the list. **Pick it once.** |
| `handles[].handle` | yes | none | Your handle, from `tiktok.com/@yourhandle`. A leading `@` is accepted and stripped. Letters, digits, `.` and `_`, 2 to 24 characters. |
| `handles[].allow_zero_followers` | no | `false` | Let a poll succeed when the account has exactly 0 followers (see below) |
| `max` | no | `5` | If more handles than this are configured, the poll fails instead of creating that many series |
| `videos` | one of `handles` or `videos` | none | A list of videos to track |
| `videos[].key` | yes | none | Permanent slug for the metric key, same rules as a handle's |
| `videos[].id` | yes | none | The numeric ID from a video's URL, `tiktok.com/@handle/video/{id}`. Digits only. Quote it, or leave it bare: either works. |
| `videos_max` | no | `20` | If more videos than this are configured, the poll fails instead of creating that many series |

**Videos are always listed by hand.** There is no way to discover an
account's videos from here. Doing so would mean calling TikTok's own
video-listing API, which is protected by request signing that a plain HTTP
client cannot produce: an unsigned call returns `HTTP 200` with an empty
body. So video IDs are always supplied directly, and tracking videos needs
no handle configured alongside them.

### Why a fresh account needs `allow_zero_followers`

By default a followers count of exactly **zero fails the poll**. A real,
tracked account is almost never at zero, so zero usually means the page was
not read properly. If you are genuinely tracking a brand-new account with no
followers yet, set `allow_zero_followers: true` on that handle's entry to
switch the guard off for it. Zero views or likes on a *video* is never
guarded this way, since a video you just posted legitimately starts at 0.

## Credentials and requests

**No credentials.** It reads public data about your own account and videos.
It never asks for a password, and it never sends or keeps a cookie.

- **Per poll:** one request per handle (its profile page), plus one request
  per video (its video page).
- **Host:** `www.tiktok.com`.
- **A documented User-Agent exception.** This is the one plugin that does
  not send the project's honest User-Agent. TikTok only serves the page data
  to a browser-shaped request, so this plugin sends one fixed, documented
  browser `User-Agent` and `Accept-Language`. It does not rotate them,
  reuse a session, or use proxies. See
  [Opt-in and inert by default](index.md#opt-in-and-inert-by-default) for
  the rules that still apply.

## When it fails

A poll is all-or-nothing: if any handle or video fails, the whole poll
fails, rather than some series updating and others silently going stale. It
fails, and never writes a partial or zero value, when:

- neither `handles` nor `videos` is configured (no request is made at all),
  or an entry is malformed, has an invalid or duplicate `key`, or has an
  invalid handle or video ID
- more handles or videos are configured than `max` or `videos_max`
- the page comes back without its data block: a captcha or interstitial
  page, a region block, or TikTok changed the page shape. Open a "Broken
  data source" issue if it persists.
- the response is larger than 5 MB, which is refused before any parsing
- **the page is for a different account or video than the one you
  configured** (a renamed or redirected handle, or TikTok serving a
  mismatched page). It fails rather than write someone else's numbers into
  your series.
- a count is missing or not numeric
- a video is deleted, private or unavailable, which fails with its own
  distinct message
- a handle's followers are exactly 0 and `allow_zero_followers` is not set

**A `403` is a network problem, not necessarily a broken plugin.** It shows
as `blocked` in `/api/plugins` and turns the dashboard dot red. TikTok
refuses some networks, cloud and datacentre address ranges especially, while
serving the same page to a home connection. Before reporting one, check
whether it reproduces from another connection.

See [When a plugin fails](index.md#when-a-plugin-fails) for how failures
show up in general.

## Stability

**Unofficial, so expect occasional breakage.** The page data isn't
documented or promised by TikTok. The project runs a daily canary against
the live source to notice shape changes early, but a change on TikTok's side
can still break polling until the plugin is updated. The
[source status](../status.md) page shows the latest canary result.
