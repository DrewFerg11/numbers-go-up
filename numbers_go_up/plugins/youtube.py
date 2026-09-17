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
"""

import json
import logging
import os
import re

import httpx

from numbers_go_up import http as http_module

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
}

_ENV_API_KEY = "NGU_YOUTUBE_API_KEY"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_DEFAULT_MAX_CHANNELS = 5

_KEY_PATTERN = re.compile(r"^[a-z0-9_-]+$")
# "UC" + 22 base64url characters -- channel IDs are case-sensitive, which
# is exactly why `key` (not the channel ID) is the metric-key slug.
_CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def _validate_max_channels(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"max must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"max must be at least 1, got {value!r}")
    return value


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

    Returns [(key, channel_id), ...], order preserved.
    """
    channels = config.get("channels")
    if not channels:
        raise ValueError("channels is not configured")
    if not isinstance(channels, list):
        raise ValueError(f"channels must be a list, got {type(channels).__name__}")

    max_channels = _validate_max_channels(config.get("max", _DEFAULT_MAX_CHANNELS))

    validated: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for entry in channels:
        if not isinstance(entry, dict):
            raise ValueError(f"channels entry must be a mapping, got {entry!r}")

        key = entry.get("key")
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
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
        raise ValueError(
            f"YouTube channel {channel_id} request failed: {status} for '{url}'"
        ) from exc

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


def collect(config: dict, http) -> dict[str, int | float | dict]:
    """config["channels"]: list of {"key": slug, "id": "UC..."}. No default
    -- the plugin makes no request at all until it's set.

    config["source"]: must be "official" (the only source this plugin
    ships). config["max"]: cardinality guard, default 5.

    The API key is read from the NGU_YOUTUBE_API_KEY env var, never from
    config, and is redacted from every exception message this module (and
    http.py's Blocked) can raise.
    """
    _validate_source(config)
    channels = _validate_channels(config)

    api_key = os.environ.get(_ENV_API_KEY)
    if not api_key:
        raise ValueError(f"{_ENV_API_KEY} is not set (required for source: official)")

    result: dict[str, int | float | dict] = {}
    for key, channel_id in channels:
        item = _fetch_channel(http, api_key, channel_id)
        result.update(_channel_metrics(key, channel_id, item))
    return result
