"""MakerWorld public profile counters, and (opt-in) per-model counters.

Opt-in: requires plugins.makerworld.user_id. Corrected Sep 11 against a live
response (see #26): there is no ``data`` wrapper -- ``MWCount`` is a
top-level key, and ``likeCount``/``collectionCount`` are top-level siblings
of ``MWCount``, not nested inside it. Top-level ``downloadCount`` exists but
is inflated relative to the real per-type counts inside ``MWCount`` -- never
use it.

Per-model stats (#55, behind ``plugins.makerworld.models.enabled``): the
same host's published-design listing, ``GET
.../v1/design-service/published/{uid}/design?offset&limit``, paged at
limit=100. Per-model ``downloadCount``/``printCount``/``likeCount`` were
cross-checked against the profile's own totals (Sep 14 2026 capture) and
matched exactly (``collectionCount`` was off by 2, timing between the two
requests) -- unlike the profile's inflated top-level ``downloadCount``, the
per-model ``downloadCount`` is the real number. ``shareCount``/``readCount``
and per-instance counters are deferred (see the issue).

Responsible Use #3 exception: with ``models.enabled``, each poll adds one
paged listing request (public data about the user's own account, no auth)
to the same host as the profile request -- next to the TikTok browser-UA
exception, this is the project's other documented departure from "one
request per poll".
"""

import json
import logging

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1800  # 30 min -- these move a few times a day

_PROFILE_METRICS = {
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

# Downloads/prints match the profile's own cumulative counters in the
# capture this was verified against; likes/collections/comments can fall
# (unlike/uncollect/delete). Kinds are sticky per series once shipped -- see
# get_or_create_series -- so these must never change.
_MODEL_METRIC_PATTERNS = {
    "makerworld.model.{id}.downloads": {
        "kind": "cumulative",
        "label": "MW Model Downloads",
        "unit": "downloads",
    },
    "makerworld.model.{id}.prints": {
        "kind": "cumulative",
        "label": "MW Model Prints",
        "unit": "prints",
    },
    "makerworld.model.{id}.likes": {
        "kind": "gauge",
        "label": "MW Model Likes",
        "unit": "likes",
    },
    "makerworld.model.{id}.collections": {
        "kind": "gauge",
        "label": "MW Model Collections",
        "unit": "collections",
    },
    "makerworld.model.{id}.comments": {
        "kind": "gauge",
        "label": "MW Model Comments",
        "unit": "comments",
    },
}

METRICS = {**_PROFILE_METRICS, **_MODEL_METRIC_PATTERNS}

# api.bambulab.com, not makerworld.com (#30): makerworld.com sits behind a
# Cloudflare bot challenge that 403s any non-browser TLS client, while the
# same user-service answers unauthenticated on the Bambu API host with an
# identical response body (verified Sep 14 2026, all 8 values matched).
_PROFILE_URL = "https://api.bambulab.com/v1/user-service/user/profile/{uid}"
_LISTING_URL = "https://api.bambulab.com/v1/design-service/published/{uid}/design"
_LISTING_PAGE_LIMIT = 100
_DEFAULT_MODELS_MAX = 50

# Model IDs from ``include`` that were absent from the listing, logged once
# per process rather than once per poll (Failure Handling: don't flood the
# log every 30 minutes for a model that was never going to reappear).
# Process-lifetime, deliberately not reset per call.
_warned_missing_model_ids: set[str] = set()


def _validate_numeric_id(value: object, what: str) -> str:
    """Validate a MakerWorld numeric id (user id or model id).

    isascii() first: isdigit() is True for Unicode digit characters
    (superscript two, Arabic-Indic, full-width) that would otherwise be
    percent-encoded into the URL. Same guard http.py's parse_retry_after
    uses for the same reason. Booleans are rejected explicitly -- ``bool``
    is an ``int`` subclass, but ``True``/``False`` are not model IDs.
    """
    if isinstance(value, bool):
        raise ValueError(f"{what} must be numeric, got {value!r}")
    text = str(value)
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"{what} must be numeric, got {value!r}")
    return text


def _validate_include(include: object) -> list[str] | None:
    """Validate ``models.include`` up front, before any request is made.

    Returns the validated list of model-id strings, or None if ``include``
    is empty/absent (meaning "every published model").
    """
    if not include:
        return None
    if not isinstance(include, list):
        raise ValueError(f"models.include must be a list, got {type(include).__name__}")
    return [_validate_numeric_id(item, "models.include entry") for item in include]


def _fetch_all_models(http, user_id: str) -> list[dict]:
    """Page through the published-design listing and return every hit.

    One request per page, ``limit=100``, until ``offset >= total``. A
    listing with zero models still makes exactly one request.
    """
    models: list[dict] = []
    offset = 0
    while True:
        response = http.get(
            _LISTING_URL.format(uid=user_id),
            params={"offset": offset, "limit": _LISTING_PAGE_LIMIT},
            timeout=15,
        )
        response.raise_for_status()
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(
                "MakerWorld listing response is not valid JSON "
                "(Cloudflare challenge/maintenance page?)"
            ) from exc

        try:
            total = body["total"]
            hits = body["hits"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"MakerWorld listing response missing expected field: {exc}"
            ) from exc
        if not isinstance(hits, list):
            raise ValueError(
                f"MakerWorld listing 'hits' must be a list, got {type(hits).__name__}"
            )

        models.extend(hits)
        offset += _LISTING_PAGE_LIMIT
        if offset >= total:
            break
    return models


def _select_models(
    models: list[dict], include_ids: list[str] | None, max_models: int
) -> dict[str, dict]:
    """Filter the listing by ``include`` (if given) and enforce ``max``.

    Returns {model_id: hit}. Raises ValueError if the selected count
    exceeds ``max_models`` -- applied *after* filtering, per the issue.
    """
    try:
        by_id = {str(model["id"]): model for model in models}
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"MakerWorld listing hit missing expected field: {exc}"
        ) from exc

    if include_ids is None:
        selected_ids = list(by_id)
    else:
        selected_ids = []
        for model_id in include_ids:
            if model_id not in by_id:
                if model_id not in _warned_missing_model_ids:
                    logger.warning(
                        "MakerWorld models.include id %s was not found in the "
                        "published-design listing; skipping",
                        model_id,
                    )
                    _warned_missing_model_ids.add(model_id)
                continue
            selected_ids.append(model_id)

    if len(selected_ids) > max_models:
        raise ValueError(
            f"MakerWorld models selected ({len(selected_ids)}) exceeds "
            f"models.max ({max_models})"
        )

    return {model_id: by_id[model_id] for model_id in selected_ids}


def _model_metrics(selected: dict[str, dict]) -> dict[str, dict]:
    """Build the {metric_key: {"value", "label", "attrs"}} dict for the
    selected models. kind/unit come only from METRICS -- never set here.
    """
    result: dict[str, dict] = {}
    for model_id, model in selected.items():
        try:
            title = model["title"]
            slug = model["slug"]
            counts = {
                "downloads": model["downloadCount"],
                "prints": model["printCount"],
                "likes": model["likeCount"],
                "collections": model["collectionCount"],
                "comments": model["commentCount"],
            }
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"MakerWorld model {model_id} missing expected field: {exc}"
            ) from exc

        attrs = {
            "model_id": int(model_id),
            "slug": slug,
            "url": f"https://makerworld.com/en/models/{model_id}",
        }
        labels = {
            "downloads": f"MW {title} Downloads",
            "prints": f"MW {title} Prints",
            "likes": f"MW {title} Likes",
            "collections": f"MW {title} Collections",
            "comments": f"MW {title} Comments",
        }
        for metric, value in counts.items():
            result[f"makerworld.model.{model_id}.{metric}"] = {
                "value": value,
                "label": labels[metric],
                "attrs": attrs,
            }
    return result


def collect(config: dict, http) -> dict[str, int | float | dict]:
    """config["user_id"]: the MakerWorld numeric user id. No default --
    the plugin makes no request at all until it's set.

    config["models"]: optional {"enabled": bool, "include": [id, ...],
    "max": int}. Absent or "enabled" falsy means exactly today's behaviour
    -- one request per poll, profile only. When enabled, a listing failure
    (bad JSON, non-200, missing fields) fails the whole poll: with a single
    listing request there is no partial per-model failure to isolate.
    """
    user_id = config.get("user_id")
    if not user_id:
        raise ValueError("user_id is not configured")
    user_id = _validate_numeric_id(user_id, "user_id")

    models_config = config.get("models")
    models_enabled = isinstance(models_config, dict) and models_config.get("enabled")
    if models_enabled:
        # Validate config up front, before any request: a bad `include`
        # entry must fail clearly without making the profile request.
        include_ids = _validate_include(models_config.get("include"))
        max_models = models_config.get("max", _DEFAULT_MODELS_MAX)

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
        result: dict[str, int | float | dict] = {
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

    if models_enabled:
        models = _fetch_all_models(http, user_id)
        selected = _select_models(models, include_ids, max_models)
        result.update(_model_metrics(selected))

    return result
