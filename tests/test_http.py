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
            transport=httpx.MockTransport(lambda r: httpx.Response(403))
        )

        response = client.get("https://example.invalid/")
        assert response.status_code == 403
        with pytest.raises(httpx.HTTPStatusError):
            response.raise_for_status()


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
