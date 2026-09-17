import email.utils
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from numbers_go_up import __version__, http


class TestBuildClient:
    def test_every_request_carries_the_exact_project_user_agent(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("user-agent"))
            return httpx.Response(200, json={})

        client = http.build_client(transport=httpx.MockTransport(handler))
        client.get("https://example.invalid/")

        assert captured == [
            f"numbers-go-up/{__version__} (+https://github.com/DrewFerg11/numbers-go-up)"
        ]

    def test_every_request_has_a_timeout(self):
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )

        assert client.timeout.connect == http.DEFAULT_TIMEOUT_SECONDS
        assert client.timeout.read == http.DEFAULT_TIMEOUT_SECONDS

    def test_no_cookies_persist_between_requests(self):
        def handler(request: httpx.Request) -> httpx.Response:
            # Every request must arrive with no Cookie header, even after
            # a previous response set one.
            assert "cookie" not in request.headers
            return httpx.Response(200, headers={"Set-Cookie": "session=abc123; Path=/"})

        client = http.build_client(transport=httpx.MockTransport(handler))

        client.get("https://example.invalid/one")
        assert len(client.cookies) == 0  # cleared right after the response
        client.get("https://example.invalid/two")
        assert len(client.cookies) == 0

    def test_no_automatic_retries_on_an_ordinary_error(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500)

        client = http.build_client(transport=httpx.MockTransport(handler))
        response = client.get("https://example.invalid/")

        assert response.status_code == 500
        assert call_count == 1

    def test_429_raises_rate_limited(self):
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(429))
        )

        with pytest.raises(http.RateLimited):
            client.get("https://example.invalid/")

    def test_429_is_recorded_with_no_retry_after(self):
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(429))
        )

        with pytest.raises(http.RateLimited) as exc_info:
            client.get("https://example.invalid/")

        assert exc_info.value.retry_after is None

    def test_ordinary_status_codes_are_left_to_raise_for_status(self):
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )

        response = client.get("https://example.invalid/")
        assert response.status_code == 404
        with pytest.raises(httpx.HTTPStatusError):
            response.raise_for_status()

    def test_403_raises_blocked(self):
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(403))
        )

        with pytest.raises(http.Blocked) as exc_info:
            client.get("https://example.invalid/")

        assert str(exc_info.value).startswith(http.BLOCKED_ERROR_PREFIX)
        assert exc_info.value.response.status_code == 403

    def test_403_with_ratelimit_remaining_zero_raises_rate_limited_not_blocked(self):
        # GitHub's primary rate limit returns HTTP 403 with
        # x-ratelimit-remaining: 0 -- generic handling, not hostname-gated,
        # so any source sending these headers gets the same treatment.
        client = http.build_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    403,
                    headers={
                        "x-ratelimit-remaining": "0",
                        "x-ratelimit-reset": "9999999999",
                    },
                )
            )
        )

        with pytest.raises(http.RateLimited) as exc_info:
            client.get("https://example.invalid/")

        assert not str(exc_info.value).startswith(http.BLOCKED_ERROR_PREFIX)

    def test_403_with_ratelimit_remaining_zero_uses_reset_as_retry_after(self):
        # http.py doesn't get to control "now" (it calls
        # parse_epoch_retry_after without one), so the reset time has to be
        # relative to the real clock, not a fixed date in the past.
        reset = datetime.now(UTC) + timedelta(seconds=300)
        client = http.build_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    403,
                    headers={
                        "x-ratelimit-remaining": "0",
                        "x-ratelimit-reset": str(int(reset.timestamp())),
                    },
                )
            )
        )

        with pytest.raises(http.RateLimited) as exc_info:
            client.get("https://example.invalid/")

        assert exc_info.value.retry_after == pytest.approx(300.0, abs=1.0)

    def test_403_with_nonzero_ratelimit_remaining_still_raises_blocked(self):
        client = http.build_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(403, headers={"x-ratelimit-remaining": "5"})
            )
        )

        with pytest.raises(http.Blocked):
            client.get("https://example.invalid/")

    def test_403_with_huge_ratelimit_reset_still_raises_rate_limited(self):
        # A bogus/huge x-ratelimit-reset must not crash the response hook
        # with an unhandled ValueError/OverflowError from inside
        # parse_epoch_retry_after -- it degrades to retry_after=None.
        client = http.build_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    403,
                    headers={
                        "x-ratelimit-remaining": "0",
                        "x-ratelimit-reset": "9" * 20,
                    },
                )
            )
        )

        with pytest.raises(http.RateLimited) as exc_info:
            client.get("https://example.invalid/")

        assert exc_info.value.retry_after is None

    def test_plain_403_with_no_ratelimit_headers_still_raises_blocked(self):
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(403))
        )

        with pytest.raises(http.Blocked):
            client.get("https://example.invalid/")

    def test_blocked_is_an_http_status_error(self):
        # Plugins that already expect a 403 to raise HTTPStatusError (via
        # raise_for_status) keep working unchanged.
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(403))
        )

        with pytest.raises(httpx.HTTPStatusError):
            client.get("https://example.invalid/")

    def test_blocked_message_redacts_a_key_query_param(self):
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(403))
        )

        with pytest.raises(http.Blocked) as exc_info:
            client.get("https://example.invalid/channels", params={"key": "secret123"})

        assert "secret123" not in str(exc_info.value)
        assert "REDACTED" in str(exc_info.value)

    def test_blocked_message_names_a_cloudflare_challenge(self):
        client = http.build_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(403, headers={"cf-mitigated": "challenge"})
            )
        )

        with pytest.raises(http.Blocked, match="cf-mitigated=challenge"):
            client.get("https://example.invalid/")

    def test_blocked_response_body_is_readable_for_the_failure_log(self):
        client = http.build_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(403, text="<title>Just a moment...</title>")
            )
        )

        with pytest.raises(http.Blocked) as exc_info:
            client.get("https://example.invalid/")

        assert "Just a moment" in exc_info.value.response.text

    def test_rate_limited_carries_the_response(self):
        client = http.build_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(429, text="slow down")
            )
        )

        with pytest.raises(http.RateLimited) as exc_info:
            client.get("https://example.invalid/")

        assert exc_info.value.response.status_code == 429
        assert exc_info.value.response.text == "slow down"


class TestRedactQueryParam:
    def test_redacts_key_param(self):
        url = httpx.URL("https://example.invalid/x?key=secret&id=123")
        result = http.redact_query_param(url)
        assert "secret" not in result
        assert "key=REDACTED" in result
        assert "id=123" in result

    def test_no_secret_param_is_unchanged(self):
        url = httpx.URL("https://example.invalid/x?id=123")
        assert http.redact_query_param(url) == str(url)


class TestParseRetryAfter:
    def test_none_returns_none(self):
        assert http.parse_retry_after(None) is None

    def test_delta_seconds_form(self):
        assert http.parse_retry_after("120") == 120.0

    def test_http_date_form(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        target = now + timedelta(seconds=120)
        header_value = email.utils.format_datetime(target, usegmt=True)

        assert http.parse_retry_after(header_value, now=now) == 120.0

    def test_unparseable_value_returns_none(self):
        assert http.parse_retry_after("not a valid value") is None

    def test_unicode_digit_characters_return_none_not_valueerror(self):
        # str.isdigit() is True for Unicode digit characters that float()
        # rejects, so a naive isdigit() check raises ValueError out of
        # parse_retry_after -- and out of the response hook, mid-request.
        # The contract is None for anything unparseable.
        assert http.parse_retry_after("²") is None
        assert http.parse_retry_after("١٢٣") is None

    def test_past_http_date_returns_zero_not_negative(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        past = now - timedelta(seconds=60)
        header_value = email.utils.format_datetime(past, usegmt=True)

        assert http.parse_retry_after(header_value, now=now) == 0.0


class TestParseEpochRetryAfter:
    def test_none_returns_none(self):
        assert http.parse_epoch_retry_after(None) is None

    def test_future_epoch_seconds(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        reset = now + timedelta(seconds=300)

        assert http.parse_epoch_retry_after(
            str(int(reset.timestamp())), now=now
        ) == pytest.approx(300.0, abs=1.0)

    def test_past_epoch_returns_zero_not_negative(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        past = now - timedelta(seconds=60)

        assert http.parse_epoch_retry_after(str(int(past.timestamp())), now=now) == 0.0

    def test_unparseable_value_returns_none(self):
        assert http.parse_epoch_retry_after("not a number") is None

    def test_unicode_digit_characters_return_none_not_valueerror(self):
        assert http.parse_epoch_retry_after("²") is None
        assert http.parse_epoch_retry_after("١٢٣") is None

    def test_huge_numeric_value_returns_none_not_raise(self):
        # All-digit strings pass the isdigit() gate but can still overflow
        # datetime.fromtimestamp (ValueError) or the platform's time_t
        # (OverflowError) -- must return None like any other unparseable
        # value, not crash the response hook every plugin's client shares.
        assert http.parse_epoch_retry_after("9" * 14) is None
        assert http.parse_epoch_retry_after("9" * 20) is None
