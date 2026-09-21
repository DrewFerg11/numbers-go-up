"""Offline tests for numbers_go_up/plugins/tiktok.py.

tests/fixtures/tiktok_profile.html and tiktok_no_userinfo.html are
**synthetic** fixtures built to the rehydration-blob schema documented in
issue #95 (verified there against a live capture during the spike), trimmed
to the ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` script plus minimal
surrounding markup, with a placeholder handle/nickname/user ID -- not a
fresh live capture committed by this change (see the PR description for why:
no live TikTok fetch happens in this environment). ``test_live_tiktok``
below is the real check once ``NGU_CANARY_TIKTOK_HANDLE`` is set.
"""

import json
import os
import pathlib

import httpx
import pytest

from numbers_go_up import http
from numbers_go_up.plugins import tiktok

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def _fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _client(handler) -> httpx.Client:
    return http.build_client(transport=httpx.MockTransport(handler))


def _handle(key: str = "canary", handle: str = "example.user") -> dict:
    return {"key": key, "handle": handle}


def _html_response(
    *, unique_id: str = "example.user", **stat_overrides
) -> httpx.Response:
    stats = {
        "followerCount": 48213,
        "followingCount": 57,
        "videoCount": 214,
        "heartCount": -1625611536,
        "diggCount": 19,
    }
    stats.update(stat_overrides)
    blob = {
        "__DEFAULT_SCOPE__": {
            "webapp.user-detail": {
                "userInfo": {
                    "user": {
                        "id": "6829267836023880194",
                        "uniqueId": unique_id,
                        "nickname": "Example User",
                    },
                    "stats": stats,
                }
            }
        }
    }
    html = (
        '<html><body><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
        f'type="application/json">{json.dumps(blob)}</script></body></html>'
    )
    return httpx.Response(200, text=html)


class TestHandlesValidation:
    def test_missing_handles_is_inert_no_request_made(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made without handles")

        with pytest.raises(ValueError, match="handles"):
            tiktok.collect({}, _client(handler))

    def test_empty_handles_is_inert_no_request_made(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with empty handles")

        with pytest.raises(ValueError, match="handles"):
            tiktok.collect({"handles": []}, _client(handler))

    def test_non_list_handles_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with non-list handles")

        with pytest.raises(ValueError, match="handles"):
            tiktok.collect({"handles": "example.user"}, _client(handler))

    def test_non_mapping_entry_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad entry")

        with pytest.raises(ValueError, match="mapping"):
            tiktok.collect({"handles": ["example.user"]}, _client(handler))

    @pytest.mark.parametrize(
        "bad_key", ["Main", "main slug", "MAIN", "", None, "main!"]
    )
    def test_invalid_key_fails_without_a_request(self, bad_key):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad key")

        with pytest.raises(ValueError, match="key"):
            tiktok.collect(
                {"handles": [{"key": bad_key, "handle": "example.user"}]},
                _client(handler),
            )

    def test_duplicate_key_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a duplicate key")

        with pytest.raises(ValueError, match="duplicate"):
            tiktok.collect(
                {
                    "handles": [
                        {"key": "main", "handle": "example.user"},
                        {"key": "main", "handle": "other.user"},
                    ]
                },
                _client(handler),
            )

    @pytest.mark.parametrize(
        "bad_handle",
        ["", None, "a" * 25, "has space", "has/slash", "has#hash", 123],
    )
    def test_invalid_handle_fails_without_a_request(self, bad_handle):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad handle")

        with pytest.raises(ValueError, match="handle"):
            tiktok.collect(
                {"handles": [{"key": "main", "handle": bad_handle}]},
                _client(handler),
            )

    def test_leading_at_sign_is_stripped(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _html_response()

        tiktok.collect(
            {"handles": [{"key": "main", "handle": "@example.user"}]},
            _client(handler),
        )

        assert requests[0].url.path == "/@example.user"

    def test_non_int_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad max")

        with pytest.raises(ValueError, match="max"):
            tiktok.collect({"handles": [_handle()], "max": "5"}, _client(handler))

    def test_bool_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bool max")

        with pytest.raises(ValueError, match="max"):
            tiktok.collect({"handles": [_handle()], "max": True}, _client(handler))

    def test_exceeding_max_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made when max is exceeded")

        with pytest.raises(ValueError, match="max"):
            tiktok.collect(
                {
                    "handles": [
                        {"key": "one", "handle": "user.one"},
                        {"key": "two", "handle": "user.two"},
                    ],
                    "max": 1,
                },
                _client(handler),
            )


class TestSuccess:
    def test_one_handle_makes_exactly_one_request_with_browser_headers(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _html_response()

        result = tiktok.collect({"handles": [_handle()]}, _client(handler))

        assert len(requests) == 1
        req = requests[0]
        assert req.url.host == "www.tiktok.com"
        assert req.url.path == "/@example.user"
        assert req.headers["user-agent"] == tiktok._BROWSER_USER_AGENT
        assert req.headers["accept-language"] == tiktok._ACCEPT_LANGUAGE

        assert set(result) == {
            "tiktok.user.canary.followers",
            "tiktok.user.canary.following",
            "tiktok.user.canary.videos",
        }
        followers = result["tiktok.user.canary.followers"]
        assert followers["value"] == 48213
        assert followers["label"] == "TT Example User Followers"
        assert followers["attrs"] == {
            "handle": "example.user",
            "url": "https://www.tiktok.com/@example.user",
            "user_id": "6829267836023880194",
        }
        assert result["tiktok.user.canary.following"]["value"] == 57
        assert result["tiktok.user.canary.videos"]["value"] == 214

    def test_two_handles_make_two_independent_requests(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/@user.one":
                return _html_response(unique_id="user.one")
            return _html_response(unique_id="user.two")

        result = tiktok.collect(
            {
                "handles": [
                    {"key": "one", "handle": "user.one"},
                    {"key": "two", "handle": "user.two"},
                ]
            },
            _client(handler),
        )

        assert len(requests) == 2
        assert set(result) == {
            "tiktok.user.one.followers",
            "tiktok.user.one.following",
            "tiktok.user.one.videos",
            "tiktok.user.two.followers",
            "tiktok.user.two.following",
            "tiktok.user.two.videos",
        }

    def test_one_of_two_handles_failing_fails_the_whole_poll(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/@user.one":
                return _html_response(unique_id="user.one")
            return httpx.Response(200, text="<html>no blob here</html>")

        with pytest.raises(ValueError):
            tiktok.collect(
                {
                    "handles": [
                        {"key": "one", "handle": "user.one"},
                        {"key": "two", "handle": "user.two"},
                    ]
                },
                _client(handler),
            )

    def test_missing_nickname_falls_back_to_key(self):
        blob = {
            "__DEFAULT_SCOPE__": {
                "webapp.user-detail": {
                    "userInfo": {
                        "user": {"id": "1", "uniqueId": "example.user"},
                        "stats": {
                            "followerCount": 10,
                            "followingCount": 1,
                            "videoCount": 1,
                        },
                    }
                }
            }
        }
        html = (
            '<html><body><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
            f'type="application/json">{json.dumps(blob)}</script></body></html>'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        result = tiktok.collect(
            {"handles": [_handle(key="nickless")]}, _client(handler)
        )

        assert (
            result["tiktok.user.nickless.followers"]["label"] == "TT nickless Followers"
        )

    def test_heart_count_is_never_in_the_result(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _html_response()

        result = tiktok.collect({"handles": [_handle()]}, _client(handler))

        for entry in result.values():
            assert "heart" not in json.dumps(entry).lower()
        assert not any("heart" in key.lower() for key in tiktok.METRICS)


class TestFailureHandling:
    def test_uniqueid_mismatch_fails_the_poll(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _html_response(unique_id="a-different-user")

        with pytest.raises(ValueError, match="does not match"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_missing_script_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body>captcha page</body></html>")

        with pytest.raises(ValueError, match="__UNIVERSAL_DATA_FOR_REHYDRATION__"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_unparseable_json_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            html = (
                '<html><body><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
                'type="application/json">{not valid json</script></body></html>'
            )
            return httpx.Response(200, text=html)

        with pytest.raises(ValueError, match="not valid JSON"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_missing_userinfo_fails_distinctly(self):
        html = _fixture_text("tiktok_no_userinfo.html")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        with pytest.raises(ValueError, match="userInfo"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_oversized_body_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            padding = "x" * (tiktok._MAX_BODY_BYTES + 1)
            return httpx.Response(200, text=f"<html><!--{padding}--></html>")

        with pytest.raises(ValueError, match="cap"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_zero_followers_fails_the_poll(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _html_response(followerCount=0)

        with pytest.raises(ValueError, match="followerCount is 0"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_missing_followers_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            blob = {
                "__DEFAULT_SCOPE__": {
                    "webapp.user-detail": {
                        "userInfo": {
                            "user": {"id": "1", "uniqueId": "example.user"},
                            "stats": {"followingCount": 1, "videoCount": 1},
                        }
                    }
                }
            }
            html = (
                '<html><body><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
                f'type="application/json">{json.dumps(blob)}</script></body></html>'
            )
            return httpx.Response(200, text=html)

        with pytest.raises(ValueError, match="followerCount is missing"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_non_numeric_following_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _html_response(followingCount="lots")

        with pytest.raises(ValueError, match="must be numeric"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_bool_video_count_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _html_response(videoCount=True)

        with pytest.raises(ValueError, match="must be numeric"):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_5xx_fails_the_whole_poll(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with pytest.raises(httpx.HTTPStatusError):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_403_is_blocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        with pytest.raises(http.Blocked):
            tiktok.collect({"handles": [_handle()]}, _client(handler))

    def test_429_is_rate_limited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "30"})

        with pytest.raises(http.RateLimited):
            tiktok.collect({"handles": [_handle()]}, _client(handler))


class TestRealCaptureFixture:
    """Runs the plugin against the synthetic-but-schema-real fixture -- see
    this module's docstring for why it's not a fresh live capture."""

    def test_mapping_against_the_profile_fixture(self):
        html = _fixture_text("tiktok_profile.html")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        result = tiktok.collect({"handles": [_handle()]}, _client(handler))

        assert result["tiktok.user.canary.followers"]["value"] == 48213
        assert result["tiktok.user.canary.following"]["value"] == 57
        assert result["tiktok.user.canary.videos"]["value"] == 214


@pytest.mark.live
def test_live_tiktok():
    handle = os.environ.get("NGU_CANARY_TIKTOK_HANDLE")
    if not handle:
        pytest.skip("NGU_CANARY_TIKTOK_HANDLE not set")

    client = http.build_client()
    try:
        result = tiktok.collect(
            {"handles": [{"key": "canary", "handle": handle}]}, client
        )
    finally:
        client.close()

    assert set(result) == {
        "tiktok.user.canary.followers",
        "tiktok.user.canary.following",
        "tiktok.user.canary.videos",
    }
    for value in result.values():
        assert isinstance(value["value"], int | float)
