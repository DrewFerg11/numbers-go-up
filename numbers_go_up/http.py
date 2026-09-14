"""The shared HTTP client every plugin uses.

Plugins never construct their own client (Responsible Use #3): an honest
User-Agent, a timeout on every request, no persisted cookies, and no
automatic retries are all enforced here once rather than trusted to every
plugin. ``raise_for_status()`` stays the plugin's own job — this module
only intercepts HTTP 429 and HTTP 403.
"""

from __future__ import annotations

import email.utils
from datetime import UTC, datetime

import httpx

from numbers_go_up import __version__

DEFAULT_TIMEOUT_SECONDS = 15.0
USER_AGENT = (
    f"numbers-go-up/{__version__} (+https://github.com/DrewFerg11/numbers-go-up)"
)


class RateLimited(Exception):
    """Raised when a request gets HTTP 429.

    Carries the parsed ``Retry-After`` in seconds, or ``None`` if the
    response didn't send one, or it couldn't be parsed.
    """

    def __init__(self, retry_after: float | None):
        super().__init__(f"rate limited (retry_after={retry_after})")
        self.retry_after = retry_after


# Every Blocked error message starts with this, so /api/plugins can tell a
# blocked source apart from an ordinary error using only plugin_runs.error.
BLOCKED_ERROR_PREFIX = "blocked"


class Blocked(httpx.HTTPStatusError):
    """Raised when a request gets HTTP 403.

    A sustained 403 is a source refusing this client (e.g. a Cloudflare
    bot challenge, see #30), not a transient error, so the scheduler backs
    off instead of hitting the same wall every interval. Subclasses
    ``HTTPStatusError`` so code that already expects ``raise_for_status()``
    to raise on a 403 keeps working unchanged.
    """

    def __init__(self, response: httpx.Response):
        challenge = response.headers.get("cf-mitigated")
        detail = f", cf-mitigated={challenge}" if challenge else ""
        url = response.request.url
        super().__init__(
            f"{BLOCKED_ERROR_PREFIX} (HTTP 403{detail}) for url '{url}'",
            request=response.request,
            response=response,
        )


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header value: delta-seconds or an HTTP-date.

    Returns seconds from ``now`` (defaulting to the real current time), or
    None if ``value`` is missing or unparseable. Never negative.
    """
    if value is None:
        return None

    value = value.strip()
    # isascii() first: isdigit() is True for Unicode digit characters
    # (e.g. "²") that float() then rejects, raising instead of returning
    # None as this function promises. ASCII-only keeps the contract.
    if value.isascii() and value.isdigit():
        return float(value)

    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    reference = now if now is not None else datetime.now(UTC)
    return max((parsed - reference).total_seconds(), 0.0)


def build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Build the one shared HTTP client every plugin's ``collect()`` uses.

    - Honest User-Agent naming the project and its URL — never a spoofed
      browser string.
    - A default timeout on every request, so nothing can hang forever.
    - No cookie persistence: a ``Set-Cookie`` from one response is cleared
      before the next request goes out, on this client or any other.
      ``httpx.Client`` keeps a cookie jar by default; Responsible Use #3
      forbids cookie/session reuse outright.
    - No retry transport: one request per poll. A failed request is a
      failed poll — the next scheduled run is the retry.
    - HTTP 429 is turned into :class:`RateLimited` and HTTP 403 into
      :class:`Blocked` here; every other status is left to the plugin's
      own ``raise_for_status()``.

    ``transport`` lets tests substitute ``httpx.MockTransport`` — no
    network access in anything this module's tests do.
    """
    client = httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT_SECONDS,
        transport=transport,
    )

    def _on_response(response: httpx.Response) -> None:
        client.cookies.clear()
        if response.status_code == 429:
            raise RateLimited(parse_retry_after(response.headers.get("Retry-After")))
        if response.status_code == 403:
            raise Blocked(response)

    client.event_hooks["response"] = [_on_response]
    return client
