"""MakerWorld public profile counters. Opt-in: requires plugins.makerworld.user_id.

Corrected Sep 11 against a live response (see #26): there is no ``data``
wrapper -- ``MWCount`` is a top-level key, and ``likeCount``/
``collectionCount`` are top-level siblings of ``MWCount``, not nested
inside it. Top-level ``downloadCount`` exists but is inflated relative to
the real per-type counts inside ``MWCount`` -- never use it.
"""

import json

POLL_INTERVAL_SECONDS = 1800  # 30 min -- these move a few times a day

METRICS = {
    "makerworld.profile.design_downloads": {
        "kind": "cumulative",
        "label": "MW Design Downloads",
        "unit": "downloads",
    },
    "makerworld.profile.instance_downloads": {
        "kind": "cumulative",
        "label": "MW Instance Downloads",
        "unit": "downloads",
    },
    "makerworld.profile.design_prints": {
        "kind": "cumulative",
        "label": "MW Design Prints",
        "unit": "prints",
    },
    "makerworld.profile.instance_prints": {
        "kind": "cumulative",
        "label": "MW Instance Prints",
        "unit": "prints",
    },
    "makerworld.profile.likes": {
        "kind": "gauge",  # can go down
        "label": "MW Likes",
        "unit": "likes",
    },
    "makerworld.profile.collections": {
        "kind": "gauge",  # can go down
        "label": "MW Collections",
        "unit": "collections",
    },
    "makerworld.profile.followers": {
        "kind": "gauge",  # followers can unfollow
        "label": "MW Followers",
        "unit": "followers",
    },
    "makerworld.profile.level": {
        # Nothing guarantees this never drops; a wrongly-declared
        # cumulative would corrupt Home Assistant's long-term statistics
        # if it ever did.
        "kind": "gauge",
        "label": "MW Level",
        "unit": "level",
    },
}

# api.bambulab.com, not makerworld.com (#30): makerworld.com sits behind a
# Cloudflare bot challenge that 403s any non-browser TLS client, while the
# same user-service answers unauthenticated on the Bambu API host with an
# identical response body (verified Sep 14 2026, all 8 values matched).
_PROFILE_URL = "https://api.bambulab.com/v1/user-service/user/profile/{uid}"


def collect(config: dict, http) -> dict[str, int | float]:
    """config["user_id"]: the MakerWorld numeric user id. No default --
    the plugin makes no request at all until it's set.
    """
    user_id = config.get("user_id")
    if not user_id:
        raise ValueError("user_id is not configured")

    user_id = str(user_id)
    # isascii() first: isdigit() is True for Unicode digit characters
    # (superscript two, Arabic-Indic, full-width) that would otherwise be
    # percent-encoded into the profile URL. Same guard http.py's
    # parse_retry_after uses for the same reason.
    if not (user_id.isascii() and user_id.isdigit()):
        raise ValueError(f"user_id must be numeric, got {user_id!r}")

    response = http.get(_PROFILE_URL.format(uid=user_id), timeout=15)
    response.raise_for_status()
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(
            "MakerWorld response is not valid JSON "
            "(Cloudflare challenge/maintenance page?)"
        ) from exc

    try:
        mw = body["MWCount"]
        return {
            "makerworld.profile.design_downloads": mw["myDesignDownloadCount"],
            "makerworld.profile.instance_downloads": mw["myInstanceDownloadCount"],
            "makerworld.profile.design_prints": mw["myDesignPrintCount"],
            "makerworld.profile.instance_prints": mw["myInstancePrintCount"],
            "makerworld.profile.likes": body["likeCount"],
            "makerworld.profile.collections": body["collectionCount"],
            "makerworld.profile.followers": body["fanCount"],
            "makerworld.profile.level": body["personal"]["userLevel"]["level"],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError(f"MakerWorld response missing expected field: {exc}") from exc
