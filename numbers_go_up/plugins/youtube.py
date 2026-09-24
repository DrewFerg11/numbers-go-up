"""YouTube plugin: subscriber, view, and video counts for your own channels.

Spike findings (#62), posted on the issue before this was written: the
originally planned ``unofficial-livecounts-api`` (v0.1.6) source is
**dropped**. Reading its source (MIT-licensed, PyPI) shows it isn't just a
rotating-User-Agent quirk -- every request is signed with three custom
headers (``X-Ajay``/``X-Catto``/``X-Midas``) derived from the current
timestamp via RIPEMD160/SHA256/SHA384, which the plugin would have to
reimplement to pass livecounts.io's bot check. That's a private
signing/anti-bot scheme, not a browser-UA exception like TikTok's --
replicating it is a materially different (and unmaintained-dependency-free
only in the sense of "we'd hand-roll our own copy") reverse-engineering
exercise than this project takes on elsewhere, so it stays out.

Only ``source: official`` ships. It uses the documented Data API v3
``channels`` endpoint, unauthenticated except for the required API key.
Per the issue's own prior research (research.md, not yet in this repo --
see the PR), the official API rounds ``subscriberCount`` to 3 significant
figures above roughly four digits; ``viewCount``/``videoCount`` are
commonly understood to be exact, though this project has not independently
live-verified that split -- the ``test_live_youtube`` canary is the real
check once ``NGU_YOUTUBE_API_KEY`` and ``NGU_CANARY_YOUTUBE_CHANNEL_ID``
are set.

A series must never switch sources automatically (store-on-change would
write a fake jump between an exact and a rounded reading), so there is no
fallback path in this file at all -- ``source: livecounts`` fails clearly
rather than silently trying anything.

**Per-video views/likes (#115).** Unlike TikTok's per-video tracking
(#113/#114), this needs no scraping workaround: the same official Data API
v3 has a ``videos`` endpoint (``part=statistics``) that takes a video id
directly and returns ``viewCount``/``likeCount``/``commentCount``, same key
and quota cost (1 unit) as ``channels.list``. Configured via an independent
``videos`` list, same shape and philosophy as ``channels`` -- and, like
TikTok's ``handles``/``videos`` split, either can be configured without the
other. ``likeCount`` is omitted from the response entirely when the
uploader has hidden it (YouTube dropped the old
``dislikeCount``-hidden-toggle boolean; there's no explicit flag to check
here), so this plugin treats a missing ``likeCount`` as "not tracked this
poll" rather than an error -- ``views`` is still required and numeric, but
unlike ``channels.subscriberCount`` there is no zero-value sanity guard on
it: a freshly uploaded video legitimately starts at 0 views.
"""

import json
import logging
import os
import re

import httpx

from numbers_go_up import http as http_module
from numbers_go_up.plugins import _helpers

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1800  # 30 min

METRICS = {
    "youtube.channel.{key}.subscribers": {
        "kind": "gauge",  # can fall (unsubscribes); official API also rounds
        "label": "YT Subscribers",
        "unit": "subscribers",
        "icon": "mdi:account-group",
    },
    "youtube.channel.{key}.views": {
        "kind": "cumulative",
        "label": "YT Views",
        "unit": "views",
        "icon": "mdi:eye",
    },
    "youtube.channel.{key}.videos": {
        "kind": "gauge",  # a video can be deleted or unlisted
        "label": "YT Videos",
        "unit": "videos",
        "icon": "mdi:video",
    },
    "youtube.video.{key}.views": {
        "kind": "cumulative",  # viewCount only rises while a video exists
        "label": "YT Views",
        "unit": "views",
        "icon": "mdi:eye",
    },
    "youtube.video.{key}.likes": {
        "kind": "gauge",  # un-likes happen; also simply absent when hidden
        "label": "YT Likes",
        "unit": "likes",
        "icon": "mdi:thumb-up",
    },
}

_ENV_API_KEY = "NGU_YOUTUBE_API_KEY"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_DEFAULT_MAX_CHANNELS = 5
_DEFAULT_MAX_VIDEOS = 20

# "UC" + 22 base64url characters -- channel IDs are case-sensitive, which
# is exactly why `key` (not the channel ID) is the metric-key slug.
_CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
# YouTube video ids: exactly 11 base64url characters, case-sensitive.
_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _validate_source(config: dict) -> None:
    source = config.get("source")
    if source == "official":
        return
    if source == "livecounts":
        raise ValueError(
            "source 'livecounts' is not supported -- dropped after the #62 "
            "spike (it would require reimplementing livecounts.io's private "
            "request-signing scheme); use source: official"
        )
    raise ValueError(f"source must be 'official', got {source!r}")


def _validate_channels(config: dict) -> list[tuple[str, str]]:
    """Validate ``channels`` up front, before any request is made.

    Returns [(key, channel_id), ...], order preserved. An absent or empty
    ``channels`` is not an error here -- since #115, channel and video
    tracking are independent (mirroring TikTok's ``handles``/``videos``
    split), and whether *something* is configured at all is checked once
    in ``collect()``.
    """
    channels = config.get("channels")
    if not channels:
        return []
    if not isinstance(channels, list):
        raise ValueError(f"channels must be a list, got {type(channels).__name__}")

    max_channels = _helpers.validate_max(
        config.get("max", _DEFAULT_MAX_CHANNELS), "max"
    )

    validated: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for entry in channels:
        if not isinstance(entry, dict):
            raise ValueError(f"channels entry must be a mapping, got {entry!r}")

        key = entry.get("key")
        if not isinstance(key, str) or not _helpers.KEY_SLUG.fullmatch(key):
            raise ValueError(f"channels entry key {key!r} must match [a-z0-9_-]+")
        if key in seen_keys:
            raise ValueError(f"channels key {key!r} is a duplicate")
        seen_keys.add(key)

        channel_id = entry.get("id")
        if not isinstance(channel_id, str) or not _CHANNEL_ID_PATTERN.fullmatch(
            channel_id
        ):
            raise ValueError(
                f"channels entry {key!r} id must match 'UC' + 22 characters, "
                f"got {channel_id!r}"
            )
        validated.append((key, channel_id))

    if len(validated) > max_channels:
        raise ValueError(
            f"channels count ({len(validated)}) exceeds max ({max_channels})"
        )
    return validated


def _validate_videos(config: dict) -> list[tuple[str, str]]:
    """Validate ``videos`` up front, before any request is made.

    Returns [(key, video_id), ...], order preserved. Unlike ``channels``,
    an absent or empty ``videos`` is not an error here -- video tracking is
    entirely optional, and whether *something* is configured at all is
    checked once in ``collect()``.
    """
    videos = config.get("videos")
    if not videos:
        return []
    if not isinstance(videos, list):
        raise ValueError(f"videos must be a list, got {type(videos).__name__}")

    max_videos = _helpers.validate_max(
        config.get("videos_max", _DEFAULT_MAX_VIDEOS), "videos_max"
    )

    validated: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for entry in videos:
        if not isinstance(entry, dict):
            raise ValueError(f"videos entry must be a mapping, got {entry!r}")

        key = entry.get("key")
        if not isinstance(key, str) or not _helpers.KEY_SLUG.fullmatch(key):
            raise ValueError(f"videos entry key {key!r} must match [a-z0-9_-]+")
        if key in seen_keys:
            raise ValueError(f"videos key {key!r} is a duplicate")
        seen_keys.add(key)

        video_id = entry.get("id")
        if not isinstance(video_id, str) or not _VIDEO_ID_PATTERN.fullmatch(video_id):
            raise ValueError(
                f"videos entry {key!r} id must match "
                f"{_VIDEO_ID_PATTERN.pattern}, got {video_id!r}"
            )
        validated.append((key, video_id))

    if len(validated) > max_videos:
        raise ValueError(
            f"videos count ({len(validated)}) exceeds videos_max ({max_videos})"
        )
    return validated


def _extract_error_reason(response: httpx.Response | None) -> str | None:
    """Best-effort read of a Google API error's ``reason`` (e.g.
    ``quotaExceeded``), so the error text names it even though the status
    code alone (403) reports as ``Blocked``. Never raises."""
    if response is None:
        return None
    try:
        body = response.json()
        return body["error"]["errors"][0]["reason"]
    except Exception:
        return None


def _fetch_channel(http, api_key: str, channel_id: str) -> dict:
    params = {"part": "statistics,snippet", "id": channel_id, "key": api_key}
    try:
        response = http.get(_CHANNELS_URL, params=params, timeout=15)
    except http_module.Blocked as exc:
        # Quota exhaustion is a 403 with body {"error": {"errors":
        # [{"reason": "quotaExceeded"}]}} -- Blocked already backs off
        # correctly, but the reason should be visible in the error text.
        reason = _extract_error_reason(exc.response)
        if reason:
            exc.args = (f"{exc.args[0]}, reason={reason}",)
        raise

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        url = http_module.redact_query_param(exc.response.url)
        status = exc.response.status_code
        # `from None`, not `from exc`: httpx.HTTPStatusError's own message
        # embeds the full, unredacted request URL (key= included), and
        # chaining it would put that back in traceback.format_exc() --
        # which is exactly what the scheduler stores in plugin_runs.error
        # and logs on an ordinary (non-403/429) failure. Everything useful
        # from exc is already in this message.
        raise ValueError(
            f"YouTube channel {channel_id} request failed: {status} for '{url}'"
        ) from None

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"YouTube channel {channel_id} response is not valid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ValueError(f"YouTube channel {channel_id} response is not a JSON object")

    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"YouTube channel {channel_id} not found (empty 'items')")
    return items[0]


def _fetch_video(http, api_key: str, video_id: str) -> dict:
    params = {"part": "statistics,snippet", "id": video_id, "key": api_key}
    try:
        response = http.get(_VIDEOS_URL, params=params, timeout=15)
    except http_module.Blocked as exc:
        reason = _extract_error_reason(exc.response)
        if reason:
            exc.args = (f"{exc.args[0]}, reason={reason}",)
        raise

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        url = http_module.redact_query_param(exc.response.url)
        status = exc.response.status_code
        raise ValueError(
            f"YouTube video {video_id} request failed: {status} for '{url}'"
        ) from None

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"YouTube video {video_id} response is not valid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ValueError(f"YouTube video {video_id} response is not a JSON object")

    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(
            f"YouTube video {video_id} not found (empty 'items' -- deleted, "
            "private, or a wrong id)"
        )
    return items[0]


def _validate_count(value: object, what: str) -> int:
    if value is None:
        raise ValueError(f"YouTube {what} is missing")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"YouTube {what} must be numeric, got {value!r}") from exc


def _channel_metrics(key: str, channel_id: str, item: dict) -> dict:
    try:
        stats = item["statistics"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"YouTube channel {key} response missing 'statistics': {exc}"
        ) from exc
    if not isinstance(stats, dict):
        raise ValueError(f"YouTube channel {key} 'statistics' is not an object")

    if stats.get("hiddenSubscriberCount"):
        raise ValueError(
            f"YouTube channel {key} ({channel_id}) hides its subscriber count "
            "(hiddenSubscriberCount); it can't be tracked"
        )

    subscribers = _validate_count(
        stats.get("subscriberCount"), f"channel {key} subscriberCount"
    )
    if subscribers == 0:
        # Sanity guard: a real, non-hidden channel is never at exactly
        # zero. Unofficial sources return zeros when they break; the
        # official API shouldn't, but the guard costs nothing to keep.
        raise ValueError(
            f"YouTube channel {key} ({channel_id}) subscriberCount is 0; "
            "refusing to write a sample"
        )
    views = _validate_count(stats.get("viewCount"), f"channel {key} viewCount")
    videos = _validate_count(stats.get("videoCount"), f"channel {key} videoCount")

    snippet = item.get("snippet")
    title = snippet.get("title") if isinstance(snippet, dict) else None
    subject = title if title else key

    attrs = {
        "channel_id": channel_id,
        "url": f"https://www.youtube.com/channel/{channel_id}",
        "source": "official",
    }
    return {
        f"youtube.channel.{key}.subscribers": {
            "value": subscribers,
            "label": f"YT {subject} Subscribers",
            "attrs": attrs,
        },
        f"youtube.channel.{key}.views": {
            "value": views,
            "label": f"YT {subject} Views",
            "attrs": attrs,
        },
        f"youtube.channel.{key}.videos": {
            "value": videos,
            "label": f"YT {subject} Videos",
            "attrs": attrs,
        },
    }


def _video_metrics(key: str, video_id: str, item: dict) -> dict:
    try:
        stats = item["statistics"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"YouTube video {key} response missing 'statistics': {exc}"
        ) from exc
    if not isinstance(stats, dict):
        raise ValueError(f"YouTube video {key} 'statistics' is not an object")

    # No zero-value sanity guard, unlike channels.subscriberCount: a
    # freshly uploaded video legitimately starts at 0 views.
    views = _validate_count(stats.get("viewCount"), f"video {key} viewCount")

    snippet = item.get("snippet")
    title = snippet.get("title") if isinstance(snippet, dict) else None
    subject = title if title else key

    attrs = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "source": "official",
    }
    result = {
        f"youtube.video.{key}.views": {
            "value": views,
            "label": f"YT {subject} Views",
            "attrs": attrs,
        },
    }

    # likeCount is simply absent from the response when the uploader has
    # hidden it -- treated as "not tracked this poll", not an error, since
    # there's no explicit boolean flag to check the way channels.json's
    # hiddenSubscriberCount is.
    raw_likes = stats.get("likeCount")
    if raw_likes is not None:
        likes = _validate_count(raw_likes, f"video {key} likeCount")
        result[f"youtube.video.{key}.likes"] = {
            "value": likes,
            "label": f"YT {subject} Likes",
            "attrs": attrs,
        }
    return result


def collect(config: dict, http) -> dict[str, int | float | dict]:
    """config["channels"]: list of {"key": slug, "id": "UC..."}, entirely
    optional and independent of ``videos`` (see #115).

    config["source"]: must be "official" (the only source this plugin
    ships). config["max"]: cardinality guard on channels, default 5.

    config["videos"]: list of {"key": slug, "id": "..."} (``id`` is a
    YouTube video's 11-character id, e.g. from its watch URL). Entirely
    optional and independent of ``channels``. config["videos_max"]:
    cardinality guard on videos, default 20.

    At least one of ``channels``/``videos`` must be configured -- otherwise
    the plugin makes no request at all.

    The API key is read from the NGU_YOUTUBE_API_KEY env var, never from
    config, and is redacted from every exception message this module (and
    http.py's Blocked) can raise.
    """
    _validate_source(config)
    if not config.get("channels") and not config.get("videos"):
        raise ValueError("channels or videos must be configured")
    channels = _validate_channels(config)
    videos = _validate_videos(config)

    api_key = os.environ.get(_ENV_API_KEY)
    if not api_key:
        raise ValueError(f"{_ENV_API_KEY} is not set (required for source: official)")

    result: dict[str, int | float | dict] = {}
    for key, channel_id in channels:
        # Deliberately all-or-nothing, like the GitHub and MakerWorld
        # plugins: one channel failing (deleted, quota, hidden count)
        # fails the whole poll, discarding any already-collected channels'
        # samples rather than partially reporting. A half-failed source
        # should look failed (#55 precedent), and the poll runner treats
        # any error this way regardless, so partial collection here would
        # buy nothing.
        item = _fetch_channel(http, api_key, channel_id)
        result.update(_channel_metrics(key, channel_id, item))
    for key, video_id in videos:
        item = _fetch_video(http, api_key, video_id)
        result.update(_video_metrics(key, video_id, item))
    return result
