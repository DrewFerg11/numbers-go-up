"""GitHub plugin: per-repo stars, forks, watchers, open issues, and
optional release-asset downloads.

The only official, documented source in the project (#61) -- the
unauthenticated REST API, with an optional token for a higher rate limit.

Field gotchas, verified against GitHub's REST API docs (Sep 2026):
watchers must come from ``subscribers_count``, never ``watchers_count``
(a legacy alias that equals stars and would silently duplicate that
series). ``open_issues_count`` includes open pull requests -- there is no
free way to split them without extra search-API calls, so it's labelled
"Open issues & PRs" rather than split.

Repos are keyed by their numeric ``id``, never ``owner/name`` (#55
precedent): a rename or transfer must not orphan the series' history.

Responsible Use #3 exception: with ``release_downloads: true``, each poll
adds ceil(releases / 100) paged requests per repo, following pagination via
the ``Link`` header (never guessed from a count) -- next to the MakerWorld
and TikTok exceptions, official and unauthenticated by default.
"""

import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1800  # 30 min

METRICS = {
    "github.repo.{id}.stars": {
        "kind": "gauge",  # can fall (unstars)
        "label": "GH Stars",
        "unit": "stars",
        "icon": "mdi:star",
    },
    "github.repo.{id}.forks": {
        "kind": "gauge",  # a fork can be deleted
        "label": "GH Forks",
        "unit": "forks",
        "icon": "mdi:source-fork",
    },
    "github.repo.{id}.watchers": {
        "kind": "gauge",
        "label": "GH Watchers",
        "unit": "watchers",
        "icon": "mdi:eye",
    },
    "github.repo.{id}.open_issues": {
        "kind": "gauge",
        "label": "GH Open issues & PRs",
        "unit": "issues",
        "icon": "mdi:alert-circle-outline",
    },
    "github.repo.{id}.release_downloads": {
        "kind": "cumulative",
        "label": "GH Release Downloads",
        "unit": "downloads",
        "icon": "mdi:download",
    },
}

_ENV_TOKEN = "NGU_GITHUB_TOKEN"
_API_HOST = "api.github.com"
_REPO_URL = f"https://{_API_HOST}/repos/{{repo}}"
_RELEASES_URL = f"https://{_API_HOST}/repos/{{repo}}/releases"
_RELEASES_PAGE_SIZE = 100
_DEFAULT_MAX_REPOS = 20
# Hard cap on release-listing pages per repo per poll -- same convention as
# MakerWorld's _MAX_LISTING_PAGES: bounds a runaway loop against a server
# bug or shape change rather than trusting the Link header forever.
_MAX_RELEASE_PAGES = 200

# owner: 1-39 chars, letters/digits/hyphen (GitHub's own username rules).
# name: 1-100 chars, letters/digits/dot/underscore/hyphen. "." and ".."
# alone are valid path segments elsewhere but never valid repo names.
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        # Only ever sent to api.github.com: every request this plugin
        # makes targets that host, and no other.
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _validate_max_repos(value: object) -> int:
    """Same convention as MakerWorld's ``_validate_max_models``: a plain
    int (bools are int subclasses but not counts), at least 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"max must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"max must be at least 1, got {value!r}")
    return value


def _validate_repos(config: dict) -> list[str]:
    """Validate ``repos`` up front, before any request is made.

    Returns the validated list, order preserved. Raises ValueError on an
    empty/missing list, a non-list, a malformed entry, a "." or ".." repo
    name, a case-insensitive duplicate, or exceeding ``max``.
    """
    repos = config.get("repos")
    if not repos:
        raise ValueError("repos is not configured")
    if not isinstance(repos, list):
        raise ValueError(f"repos must be a list, got {type(repos).__name__}")

    max_repos = _validate_max_repos(config.get("max", _DEFAULT_MAX_REPOS))

    validated: list[str] = []
    seen: set[str] = set()
    for entry in repos:
        if not isinstance(entry, str) or not _REPO_PATTERN.fullmatch(entry):
            raise ValueError(f"repos entry {entry!r} is not a valid 'owner/name'")
        name = entry.split("/", 1)[1]
        if name in (".", ".."):
            raise ValueError(f"repos entry {entry!r} has an invalid repo name")
        key = entry.lower()
        if key in seen:
            raise ValueError(f"repos entry {entry!r} is a duplicate")
        seen.add(key)
        validated.append(entry)

    if len(validated) > max_repos:
        raise ValueError(f"repos count ({len(validated)}) exceeds max ({max_repos})")
    return validated


def _raise_named(repo: str, what: str, exc: httpx.HTTPStatusError) -> None:
    raise ValueError(f"GitHub {what} for {repo} failed: {exc}") from exc


def _fetch_repo(http, headers: dict[str, str], repo: str) -> dict:
    response = http.get(
        _REPO_URL.format(repo=repo), headers=headers, timeout=15, follow_redirects=True
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_named(repo, "repo lookup", exc)
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"GitHub repo {repo} response is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError(f"GitHub repo {repo} response is not a JSON object")
    return body


def _repo_attrs(repo: str, body: dict) -> tuple[int, dict]:
    try:
        repo_id = body["id"]
        full_name = body["full_name"]
        html_url = body["html_url"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"GitHub repo {repo} response missing expected field: {exc}"
        ) from exc
    if isinstance(repo_id, bool) or not isinstance(repo_id, int):
        raise ValueError(f"GitHub repo {repo} 'id' must be an int, got {repo_id!r}")
    return repo_id, {"repo_id": repo_id, "full_name": full_name, "url": html_url}


def _repo_metrics(repo: str, body: dict) -> tuple[int, dict]:
    repo_id, attrs = _repo_attrs(repo, body)
    try:
        counts = {
            "stars": body["stargazers_count"],
            "forks": body["forks_count"],
            "watchers": body["subscribers_count"],
            "open_issues": body["open_issues_count"],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"GitHub repo {repo} response missing expected field: {exc}"
        ) from exc

    labels = {
        "stars": f"GH {attrs['full_name']} Stars",
        "forks": f"GH {attrs['full_name']} Forks",
        "watchers": f"GH {attrs['full_name']} Watchers",
        "open_issues": f"GH {attrs['full_name']} Open issues & PRs",
    }
    result = {
        f"github.repo.{repo_id}.{metric}": {
            "value": value,
            "label": labels[metric],
            "attrs": attrs,
        }
        for metric, value in counts.items()
    }
    return repo_id, result


def _parse_link_next(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        match = _LINK_NEXT.search(part)
        if match:
            return match.group(1)
    return None


def _fetch_release_downloads(http, headers: dict[str, str], repo: str) -> int:
    """Sum ``assets[].download_count`` across every page of releases,
    following ``Link: rel="next"`` -- never guessing page counts."""
    total = 0
    url = _RELEASES_URL.format(repo=repo)
    params: dict[str, object] | None = {"per_page": _RELEASES_PAGE_SIZE}

    for _ in range(_MAX_RELEASE_PAGES):
        response = http.get(
            url, headers=headers, params=params, timeout=15, follow_redirects=True
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_named(repo, "releases", exc)
        try:
            releases = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"GitHub releases for {repo} response is not valid JSON"
            ) from exc
        if not isinstance(releases, list):
            raise ValueError(f"GitHub releases for {repo} response is not a list")

        for release in releases:
            try:
                assets = release["assets"]
            except (KeyError, TypeError) as exc:
                raise ValueError(
                    f"GitHub release for {repo} missing 'assets': {exc}"
                ) from exc
            if not isinstance(assets, list):
                raise ValueError(f"GitHub release assets for {repo} is not a list")
            for asset in assets:
                try:
                    total += asset["download_count"]
                except (KeyError, TypeError) as exc:
                    raise ValueError(
                        f"GitHub release asset for {repo} missing "
                        f"'download_count': {exc}"
                    ) from exc

        next_url = _parse_link_next(response.headers.get("Link"))
        if not next_url:
            break
        # The token (when set) rides in `headers` on every page, including
        # this manually-followed one -- follow_redirects doesn't help here
        # since this isn't a redirect, so a Link header pointing off
        # api.github.com (a compromised or misbehaving front end) must not
        # silently receive it.
        if urlparse(next_url).hostname != _API_HOST:
            raise ValueError(
                f"GitHub releases for {repo}: refusing to follow a Link "
                f"'next' URL off {_API_HOST}: {next_url!r}"
            )
        url = next_url
        params = None  # the next URL already carries its own query string
    else:
        raise ValueError(
            f"GitHub releases for {repo} did not finish within "
            f"{_MAX_RELEASE_PAGES} pages; refusing to keep paging"
        )
    return total


def collect(config: dict, http) -> dict[str, int | float | dict]:
    """config["repos"]: list of "owner/name" strings. No default -- the
    plugin makes no request at all until it's set.

    config["release_downloads"]: opt-in bool, adds one paged releases
    listing per repo per poll (false = exactly one request per repo).

    config["max"]: cardinality guard on repos, default 20.

    A token is read from the NGU_GITHUB_TOKEN env var, never from config,
    and sent only to api.github.com.
    """
    repos = _validate_repos(config)

    release_downloads = config.get("release_downloads", False)
    if not isinstance(release_downloads, bool):
        raise ValueError(f"release_downloads must be a bool, got {release_downloads!r}")

    headers = _headers(os.environ.get(_ENV_TOKEN))

    result: dict[str, int | float | dict] = {}
    for repo in repos:
        body = _fetch_repo(http, headers, repo)
        repo_id, metrics = _repo_metrics(repo, body)
        result.update(metrics)

        if release_downloads:
            downloads = _fetch_release_downloads(http, headers, repo)
            _, attrs = _repo_attrs(repo, body)
            result[f"github.repo.{repo_id}.release_downloads"] = {
                "value": downloads,
                "label": f"GH {attrs['full_name']} Release Downloads",
                "attrs": attrs,
            }

    return result
