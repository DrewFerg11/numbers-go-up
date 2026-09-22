"""TikTok plugin: followers/following/video count for your own handle(s),
and views/likes for individual videos, via the profile-page and video-page
rehydration blobs (no library, no auth, no cookies).

**The technique** (verified against a live capture, see #95): ``GET
https://www.tiktok.com/@{handle}`` returns an HTML page embedding
``<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"
type="application/json">{...}</script>``. The blob at
``data["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]`` carries
``user`` (identity) and ``stats`` (counters). This module locates that one
``<script>`` element by its fixed ``id`` and ``json.loads`` its contents --
it never regexes the JSON itself, only the tag boundaries around it.

**Per-video views/likes** (see #113): the same technique applied to
``GET https://www.tiktok.com/@i/video/{id}`` -- ``@i`` is a placeholder,
not a real handle; TikTok resolves this page purely by the numeric video
id (verified against a live capture), so no handle is needed in config.
The blob's ``data["__DEFAULT_SCOPE__"]["webapp.video-detail"]`` carries
``itemInfo.itemStruct`` with ``stats.playCount`` (views) and
``stats.diggCount`` (likes -- this is a different, per-video field from
the account-level ``heartCount`` below, and not known to share its
overflow problem).

Video ids can't be discovered from this plugin -- that's exactly what
TikTok's own item-list API does, and it's gated behind request signing
(``X-Bogus``/``_signature``) computed by obfuscated client JS. A direct
call without that signature returns ``HTTP 200`` with an empty body --
silent bot detection, confirmed live during #113's investigation.
Replicating that signing would be a fundamentally different (and far more
fragile) undertaking than this module's one honest, unsigned GET, so video
ids are always supplied directly in config, same as handles.

**Responsible Use #3 exception.** The rehydration blob is only served for a
browser-shaped request: this module sends one fixed, documented ``User-Agent``
plus ``Accept-Language`` on every request to ``tiktok.com``, next to
MakerWorld's Cloudflare-host workaround as the project's other documented
departure from an honest UA. No rotation, no cookies (the shared client
already clears them every response -- see http.py), no session reuse, no
proxies. Public data about your own account(s) and videos; opt-in and inert
by default, same as every other plugin here.

**Do NOT track account-level likes.** ``stats.heartCount`` (the profile's
total-likes-received counter) overflows a signed int32 in practice (a live
capture during the spike returned a negative value for a large account) --
it is deliberately absent from both ``METRICS`` and ``collect()`` and must
stay that way. This does not apply to a single video's ``stats.diggCount``
above, which is a much smaller, unrelated field.

**Handle/id mismatch is a failed poll, not a silent write.** TikTok serves
a different (or generic) profile/video for a renamed, redirected, deleted,
or nonexistent handle/id; if the blob's ``user.uniqueId`` or ``itemStruct.id``
doesn't match what was configured, this counts as a failure so an existing
series is never corrupted with another account's or video's numbers.

**Cap on the response body.** This is an HTML page from an origin openly
hostile to scraping, not a small, well-behaved API response -- a captcha
page, an error page, or a future markup change could balloon in size.
Responses over :data:`_MAX_BODY_BYTES` are rejected before any parsing is
attempted.

**Display rounding above ~1M** (see the README's TikTok section): like
YouTube's official API, large accounts' stats are display-rounded on the
page itself, which this module reads verbatim -- not a bug here.
"""

import json
import logging
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1800  # 30 min

METRICS = {
    "tiktok.user.{key}.followers": {
        "kind": "gauge",  # unfollows happen
        "label": "TT Followers",
        "unit": "followers",
        "icon": "mdi:account-multiple",
    },
    "tiktok.user.{key}.following": {
        "kind": "gauge",
        "label": "TT Following",
        "unit": "following",
        "icon": "mdi:account-arrow-right",
    },
    "tiktok.user.{key}.videos": {
        "kind": "gauge",  # a video can be deleted
        "label": "TT Videos",
        "unit": "videos",
        "icon": "mdi:video",
    },
    "tiktok.video.{key}.views": {
        "kind": "cumulative",  # playCount only rises while a video exists
        "label": "TT Views",
        "unit": "views",
        "icon": "mdi:eye",
    },
    "tiktok.video.{key}.likes": {
        "kind": "gauge",  # un-likes happen, same as unfollows/unstars
        "label": "TT Likes",
        "unit": "likes",
        "icon": "mdi:heart",
    },
}

_DEFAULT_MAX_HANDLES = 5
_DEFAULT_MAX_VIDEOS = 20

# Metric-key slug: Home-Assistant-entity-id-safe, permanent once picked --
# same convention as youtube.py's channel `key` (#62/#95 precedent), because
# a TikTok handle may contain '.' and uppercase, neither of which survives
# this charset.
_KEY_PATTERN = re.compile(r"^[a-z0-9_-]+$")

# TikTok handles: letters, digits, '.', '_', 2-24 characters (a leading '@'
# is accepted and stripped, never required).
_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._]{2,24}$")

# TikTok video (aweme) ids are numeric; current ones run 19 digits, but this
# is generous rather than pinned to today's exact length.
_VIDEO_ID_PATTERN = re.compile(r"^[0-9]{5,25}$")

_PROFILE_URL_TEMPLATE = "https://www.tiktok.com/@{handle}"
# "@i" is a placeholder handle segment, not a real one -- see the module
# docstring's "Per-video views/likes" note. Verified live: TikTok serves
# the correct video page from this URL regardless of the handle segment.
_VIDEO_URL_TEMPLATE = "https://www.tiktok.com/@i/video/{id}"

# One fixed, documented browser UA + Accept-Language -- see the module
# docstring's Responsible Use #3 note. Never rotated.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

_REHYDRATION_SCRIPT_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
# Locates only the tag boundaries -- the JSON itself is always parsed with
# json.loads, never regexed. DOTALL: the blob is minified but may still
# embed literal newlines inside string values.
_SCRIPT_PATTERN = re.compile(
    r'<script\s+id="' + _REHYDRATION_SCRIPT_ID + r'"[^>]*>(.*?)</script>',
    re.DOTALL,
)

# See the module docstring's "Cap on the response body" note. Generous for
# a real profile page (typically well under 1 MB) but far below what an
# unbounded hostile response could otherwise force into memory.
_MAX_BODY_BYTES = 5_000_000


def _validate_max_handles(value: object) -> int:
    """Same convention as every other plugin's ``max``: a plain int (bools
    are int subclasses but not counts), at least 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"max must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"max must be at least 1, got {value!r}")
    return value


def _validate_max_videos(value: object) -> int:
    """Same convention as ``_validate_max_handles``, for ``videos_max`` --
    a separate key rather than reusing ``max``, since ``max`` already means
    "cardinality guard on handles" and changing that would be a breaking
    change for existing configs."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"videos_max must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"videos_max must be at least 1, got {value!r}")
    return value


def _validate_handles(config: dict) -> list[dict]:
    """Validate ``handles`` up front, before any request is made.

    Returns the validated list of {"key", "handle", "allow_zero_followers"}
    dicts, order preserved (a leading '@' already stripped from ``handle``).
    Raises ValueError on a missing/empty/non-list ``handles``, a malformed
    entry, an invalid or duplicate ``key``, a bad ``handle``, a non-bool
    ``allow_zero_followers``, or exceeding ``max``.
    """
    handles = config.get("handles")
    if not handles:
        raise ValueError("handles is not configured")
    if not isinstance(handles, list):
        raise ValueError(f"handles must be a list, got {type(handles).__name__}")

    max_handles = _validate_max_handles(config.get("max", _DEFAULT_MAX_HANDLES))

    validated: list[dict] = []
    seen_keys: set[str] = set()
    for entry in handles:
        if not isinstance(entry, dict):
            raise ValueError(f"handles entry must be a mapping, got {entry!r}")

        key = entry.get("key")
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            raise ValueError(f"handles entry key {key!r} must match [a-z0-9_-]+")
        if key in seen_keys:
            raise ValueError(f"handles key {key!r} is a duplicate")
        seen_keys.add(key)

        raw_handle = entry.get("handle")
        if not isinstance(raw_handle, str) or not raw_handle:
            raise ValueError(
                f"handles[{key!r}].handle must be a non-empty string, "
                f"got {raw_handle!r}"
            )
        handle = raw_handle[1:] if raw_handle.startswith("@") else raw_handle
        if not _HANDLE_PATTERN.fullmatch(handle):
            raise ValueError(
                f"handles[{key!r}].handle {raw_handle!r} must match "
                f"{_HANDLE_PATTERN.pattern} (a leading '@' is optional)"
            )

        allow_zero_followers = entry.get("allow_zero_followers", False)
        if not isinstance(allow_zero_followers, bool):
            raise ValueError(
                f"handles[{key!r}].allow_zero_followers must be a bool, "
                f"got {allow_zero_followers!r}"
            )

        validated.append(
            {
                "key": key,
                "handle": handle,
                "allow_zero_followers": allow_zero_followers,
            }
        )

    if len(validated) > max_handles:
        raise ValueError(
            f"handles count ({len(validated)}) exceeds max ({max_handles})"
        )
    return validated


def _validate_videos(config: dict) -> list[dict]:
    """Validate ``videos`` up front, before any request is made.

    Returns the validated list of {"key", "id"} dicts, order preserved
    (``id`` normalized to a string). Unlike ``handles``, an absent or empty
    ``videos`` is not an error here -- video tracking is entirely optional,
    and whether *something* is configured at all is checked once in
    ``collect()``. Raises ValueError on a non-list ``videos``, a malformed
    entry, an invalid or duplicate ``key``, a bad ``id``, or exceeding
    ``videos_max``.
    """
    videos = config.get("videos")
    if not videos:
        return []
    if not isinstance(videos, list):
        raise ValueError(f"videos must be a list, got {type(videos).__name__}")

    max_videos = _validate_max_videos(config.get("videos_max", _DEFAULT_MAX_VIDEOS))

    validated: list[dict] = []
    seen_keys: set[str] = set()
    for entry in videos:
        if not isinstance(entry, dict):
            raise ValueError(f"videos entry must be a mapping, got {entry!r}")

        key = entry.get("key")
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            raise ValueError(f"videos entry key {key!r} must match [a-z0-9_-]+")
        if key in seen_keys:
            raise ValueError(f"videos key {key!r} is a duplicate")
        seen_keys.add(key)

        raw_id = entry.get("id")
        # Both an int and a str are accepted -- YAML parses a bare numeric
        # id as an int, and Python ints are arbitrary-precision (unlike
        # JS numbers), so this loses no precision even at 19 digits.
        if isinstance(raw_id, bool) or not isinstance(raw_id, str | int):
            raise ValueError(
                f"videos[{key!r}].id must be a string or int, got {raw_id!r}"
            )
        video_id = str(raw_id)
        if not _VIDEO_ID_PATTERN.fullmatch(video_id):
            raise ValueError(
                f"videos[{key!r}].id {raw_id!r} must match "
                f"{_VIDEO_ID_PATTERN.pattern} (digits only)"
            )

        validated.append({"key": key, "id": video_id})

    if len(validated) > max_videos:
        raise ValueError(
            f"videos count ({len(validated)}) exceeds videos_max ({max_videos})"
        )
    return validated


def _fetch_user_info(http, handle: str) -> dict:
    """Fetch and parse the profile page for ``handle``, returning its
    ``userInfo`` object. Raises ValueError, never crashes, on anything the
    page could plausibly do short of a real HTTP error (which the shared
    client's ``raise_for_status`` / ``Blocked`` / ``RateLimited`` already
    cover)."""
    url = _PROFILE_URL_TEMPLATE.format(handle=quote(handle, safe=""))
    response = http.get(
        url,
        headers={
            "User-Agent": _BROWSER_USER_AGENT,
            "Accept-Language": _ACCEPT_LANGUAGE,
        },
        timeout=15,
    )
    response.raise_for_status()

    if len(response.content) > _MAX_BODY_BYTES:
        raise ValueError(
            f"TikTok profile @{handle} response exceeds the "
            f"{_MAX_BODY_BYTES}-byte cap; refusing to parse it"
        )

    match = _SCRIPT_PATTERN.search(response.text)
    if match is None:
        raise ValueError(
            f"TikTok profile @{handle} response has no "
            f"{_REHYDRATION_SCRIPT_ID} script -- captcha/interstitial page, "
            "region block, or TikTok changed the page shape"
        )

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"TikTok profile @{handle} {_REHYDRATION_SCRIPT_ID} script is "
            "not valid JSON"
        ) from exc

    try:
        user_info = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]
    except (KeyError, TypeError):
        raise ValueError(
            f"TikTok profile @{handle} response has no userInfo -- deleted, "
            "renamed, private, or region-blocked account"
        ) from None
    if not isinstance(user_info, dict):
        raise ValueError(f"TikTok profile @{handle} userInfo is not an object")
    return user_info


def _fetch_video_info(http, video_id: str) -> dict:
    """Fetch and parse the video page for ``video_id``, returning its
    ``itemStruct`` object. Raises ValueError, never crashes, on anything
    the page could plausibly do short of a real HTTP error (which the
    shared client's ``raise_for_status`` / ``Blocked`` / ``RateLimited``
    already cover). See the module docstring's "Per-video views/likes"
    note for why the URL's handle segment is a fixed placeholder."""
    url = _VIDEO_URL_TEMPLATE.format(id=video_id)
    response = http.get(
        url,
        headers={
            "User-Agent": _BROWSER_USER_AGENT,
            "Accept-Language": _ACCEPT_LANGUAGE,
        },
        timeout=15,
    )
    response.raise_for_status()

    if len(response.content) > _MAX_BODY_BYTES:
        raise ValueError(
            f"TikTok video {video_id} response exceeds the "
            f"{_MAX_BODY_BYTES}-byte cap; refusing to parse it"
        )

    match = _SCRIPT_PATTERN.search(response.text)
    if match is None:
        raise ValueError(
            f"TikTok video {video_id} response has no "
            f"{_REHYDRATION_SCRIPT_ID} script -- captcha/interstitial page, "
            "region block, or TikTok changed the page shape"
        )

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"TikTok video {video_id} {_REHYDRATION_SCRIPT_ID} script is not valid JSON"
        ) from exc

    try:
        video_detail = data["__DEFAULT_SCOPE__"]["webapp.video-detail"]
    except (KeyError, TypeError):
        raise ValueError(
            f"TikTok video {video_id} response has no webapp.video-detail"
        ) from None
    if not isinstance(video_detail, dict):
        raise ValueError(
            f"TikTok video {video_id} webapp.video-detail is not an object"
        )

    # A deleted/private/region-blocked/nonexistent video has no itemInfo at
    # all (verified live: statusCode is a nonzero TikTok-internal code with
    # a statusMsg like "status_deleted", itemInfo is simply absent) rather
    # than an itemInfo with empty/null stats -- so this is the one check
    # that distinguishes "video gone" from "response shape changed".
    item_info = video_detail.get("itemInfo")
    if not isinstance(item_info, dict):
        raise ValueError(
            f"TikTok video {video_id} is unavailable (statusCode="
            f"{video_detail.get('statusCode')!r}, statusMsg="
            f"{video_detail.get('statusMsg')!r}) -- deleted, private, or "
            "region-blocked"
        )

    item_struct = item_info.get("itemStruct")
    if not isinstance(item_struct, dict):
        raise ValueError(f"TikTok video {video_id} itemInfo has no itemStruct")
    return item_struct


def _validate_stat(value: object, what: str, subject: str) -> int | float:
    """``subject`` is the error message's prefix after "TikTok " -- e.g.
    ``f"profile @{handle}"`` or ``f"video {video_id}"`` -- so this one
    helper covers both the account and per-video stat fields."""
    if value is None:
        raise ValueError(f"TikTok {subject} {what} is missing")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"TikTok {subject} {what} must be numeric, got {value!r}")
    return value


def _handle_metrics(
    key: str, handle: str, user_info: dict, *, allow_zero_followers: bool = False
) -> dict:
    try:
        user = user_info["user"]
        stats = user_info["stats"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"TikTok profile @{handle} userInfo missing 'user' or 'stats': {exc}"
        ) from exc
    if not isinstance(user, dict) or not isinstance(stats, dict):
        raise ValueError(f"TikTok profile @{handle} 'user'/'stats' is not an object")

    unique_id = user.get("uniqueId")
    if not isinstance(unique_id, str) or unique_id.lower() != handle.lower():
        raise ValueError(
            f"TikTok profile @{handle} response uniqueId {unique_id!r} does "
            "not match the configured handle -- refusing (renamed, "
            "redirected, or a different account served)"
        )

    profile = f"profile @{handle}"
    followers = _validate_stat(stats.get("followerCount"), "followerCount", profile)
    if followers == 0 and not allow_zero_followers:
        # Sanity guard, same precedent as youtube.py's subscriberCount==0
        # check: a real, tracked account is never at exactly zero followers
        # -- unless its owner opted out via allow_zero_followers, because
        # they're actually tracking a fresh/inactive account.
        raise ValueError(
            f"TikTok profile @{handle} followerCount is 0; refusing to write "
            "a sample (set handles[].allow_zero_followers: true if this "
            "account genuinely has 0 followers)"
        )
    following = _validate_stat(stats.get("followingCount"), "followingCount", profile)
    videos = _validate_stat(stats.get("videoCount"), "videoCount", profile)

    nickname = user.get("nickname")
    subject = nickname if isinstance(nickname, str) and nickname else key

    attrs = {
        "handle": handle,
        "url": f"https://www.tiktok.com/@{handle}",
        "user_id": user.get("id"),
    }
    return {
        f"tiktok.user.{key}.followers": {
            "value": followers,
            "label": f"TT {subject} Followers",
            "attrs": attrs,
        },
        f"tiktok.user.{key}.following": {
            "value": following,
            "label": f"TT {subject} Following",
            "attrs": attrs,
        },
        f"tiktok.user.{key}.videos": {
            "value": videos,
            "label": f"TT {subject} Videos",
            "attrs": attrs,
        },
    }


def _video_metrics(key: str, video_id: str, item_struct: dict) -> dict:
    item_id = item_struct.get("id")
    if not isinstance(item_id, str) or item_id != video_id:
        raise ValueError(
            f"TikTok video {video_id} response id {item_id!r} does not "
            "match the configured id -- refusing (TikTok changed the page "
            "shape or served the wrong video)"
        )

    stats = item_struct.get("stats")
    if not isinstance(stats, dict):
        raise ValueError(f"TikTok video {video_id} 'stats' is not an object")

    video = f"video {video_id}"
    views = _validate_stat(stats.get("playCount"), "playCount", video)
    likes = _validate_stat(stats.get("diggCount"), "diggCount", video)

    author = item_struct.get("author")
    handle = author.get("uniqueId") if isinstance(author, dict) else None
    # Label uses the config key, not the video's caption -- captions are
    # free text (length, emoji, hashtags) that make a poor, unstable label
    # compared to a key the user picked deliberately, unlike a profile's
    # short nickname.
    attrs = {
        "video_id": video_id,
        "url": f"https://www.tiktok.com/@{handle}/video/{video_id}"
        if isinstance(handle, str) and handle
        else f"https://www.tiktok.com/@i/video/{video_id}",
        "handle": handle,
    }
    return {
        f"tiktok.video.{key}.views": {
            "value": views,
            "label": f"TT {key} Views",
            "attrs": attrs,
        },
        f"tiktok.video.{key}.likes": {
            "value": likes,
            "label": f"TT {key} Likes",
            "attrs": attrs,
        },
    }


def collect(config: dict, http) -> dict[str, int | float | dict]:
    """config["handles"]: list of {"key", "handle", "allow_zero_followers"}
    (a leading '@' on ``handle`` is accepted and stripped;
    ``allow_zero_followers`` is optional, default false).

    config["max"]: cardinality guard on handles, default 5.

    config["videos"]: list of {"key", "id"} (``id`` is a TikTok video's
    numeric id, digits only -- accepted as either a YAML int or string).
    Entirely optional and independent of ``handles``: this plugin can
    track videos with no handle configured, or vice versa, or both.
    Unlike handles, a video's id can't be discovered here (see the module
    docstring's "Per-video views/likes" note), so it's always supplied
    directly.

    config["videos_max"]: cardinality guard on videos, default 20.

    At least one of ``handles``/``videos`` must be configured -- otherwise
    the plugin makes no request at all. Exactly one GET per handle (its
    profile page) plus one GET per video (its video page). Any handle or
    video failing (HTTP error, missing/unparseable rehydration blob, an
    id/uniqueId that doesn't match what was configured, a missing/non-
    numeric stat, or -- handles only -- a zero followerCount without
    ``allow_zero_followers: true``) fails the whole poll -- both handles
    and videos come from config, not discovery, so nothing is ever
    deactivated here.
    """
    if not config.get("handles") and not config.get("videos"):
        raise ValueError("handles or videos must be configured")

    handles = _validate_handles(config) if config.get("handles") else []
    videos = _validate_videos(config)

    result: dict[str, int | float | dict] = {}
    for entry in handles:
        key = entry["key"]
        handle = entry["handle"]
        # Deliberately all-or-nothing, same precedent as every other
        # plugin here: one handle failing fails the whole poll rather than
        # partially reporting.
        user_info = _fetch_user_info(http, handle)
        result.update(
            _handle_metrics(
                key,
                handle,
                user_info,
                allow_zero_followers=entry["allow_zero_followers"],
            )
        )
    for entry in videos:
        key = entry["key"]
        video_id = entry["id"]
        item_struct = _fetch_video_info(http, video_id)
        result.update(_video_metrics(key, video_id, item_struct))
    return result
