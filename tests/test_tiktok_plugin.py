"""Offline tests for numbers_go_up/plugins/tiktok.py.

tests/fixtures/tiktok_profile.html and tiktok_no_userinfo.html are
**synthetic** fixtures built to the rehydration-blob schema documented in
issue #95 (verified there against a live capture during the spike), trimmed
to the ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` script plus minimal
surrounding markup, with a placeholder handle/nickname/user ID -- not a
fresh live capture committed by this change (see the PR description for why:
no live TikTok fetch happens in this environment). ``test_live_tiktok``
below is the real check once ``NGU_CANARY_TIKTOK_HANDLE`` is set.

tests/fixtures/tiktok_video.html and tiktok_video_unavailable.html are the
same kind of synthetic fixture, built to the ``webapp.video-detail`` schema
documented in issue #113 (verified there against a live capture of a real
video page and of ``/video/1``'s "deleted" response), with a placeholder
video id/caption/author.
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


def _video(key: str = "clip", video_id: str = "7123456789012345678") -> dict:
    return {"key": key, "id": video_id}


def _video_html_response(
    *,
    item_id: str = "7123456789012345678",
    author_unique_id: str | None = "example.user",
    **stat_overrides,
) -> httpx.Response:
    stats = {
        "diggCount": 378600,
        "shareCount": 2403,
        "commentCount": 22600,
        "playCount": 3800000,
        "collectCount": "60621",
    }
    stats.update(stat_overrides)
    item_struct = {
        "id": item_id,
        "desc": "placeholder caption",
        "stats": stats,
    }
    if author_unique_id is not None:
        item_struct["author"] = {"uniqueId": author_unique_id}
    blob = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {
                "statusCode": 0,
                "itemInfo": {"itemStruct": item_struct},
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
        ["", "a", None, "a" * 25, "has space", "has/slash", "has#hash", 123],
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

    @pytest.mark.parametrize("bad_value", ["true", 1, None])
    def test_non_bool_allow_zero_followers_fails_without_a_request(self, bad_value):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                "no request should be made with a bad allow_zero_followers"
            )

        with pytest.raises(ValueError, match="allow_zero_followers"):
            tiktok.collect(
                {
                    "handles": [
                        {
                            "key": "main",
                            "handle": "example.user",
                            "allow_zero_followers": bad_value,
                        }
                    ]
                },
                _client(handler),
            )

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


class TestVideosValidation:
    def test_neither_handles_nor_videos_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with an empty config")

        with pytest.raises(ValueError, match="handles or videos"):
            tiktok.collect({}, _client(handler))

    def test_missing_videos_with_handles_configured_is_fine(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _html_response()

        result = tiktok.collect({"handles": [_handle()]}, _client(handler))

        assert not any(key.startswith("tiktok.video.") for key in result)

    def test_non_list_videos_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with non-list videos")

        with pytest.raises(ValueError, match="videos"):
            tiktok.collect({"videos": "7123456789012345678"}, _client(handler))

    def test_non_mapping_entry_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad entry")

        with pytest.raises(ValueError, match="mapping"):
            tiktok.collect({"videos": ["7123456789012345678"]}, _client(handler))

    @pytest.mark.parametrize(
        "bad_key", ["Clip", "clip slug", "CLIP", "", None, "clip!"]
    )
    def test_invalid_key_fails_without_a_request(self, bad_key):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad key")

        with pytest.raises(ValueError, match="key"):
            tiktok.collect(
                {"videos": [{"key": bad_key, "id": "7123456789012345678"}]},
                _client(handler),
            )

    def test_duplicate_key_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a duplicate key")

        with pytest.raises(ValueError, match="duplicate"):
            tiktok.collect(
                {
                    "videos": [
                        {"key": "clip", "id": "7123456789012345678"},
                        {"key": "clip", "id": "7000000000000000000"},
                    ]
                },
                _client(handler),
            )

    @pytest.mark.parametrize(
        "bad_id",
        ["", "123", "a" * 26, "not-digits", None, 1.5, True, "71234567890123456 78"],
    )
    def test_invalid_id_fails_without_a_request(self, bad_id):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad id")

        with pytest.raises(ValueError, match="id"):
            tiktok.collect(
                {"videos": [{"key": "clip", "id": bad_id}]}, _client(handler)
            )

    def test_int_id_is_accepted_and_normalized_to_a_string(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _video_html_response(item_id="7123456789012345678")

        tiktok.collect(
            {"videos": [{"key": "clip", "id": 7123456789012345678}]},
            _client(handler),
        )

        assert requests[0].url.path == "/@i/video/7123456789012345678"

    def test_non_int_videos_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a non-int videos_max")

        with pytest.raises(ValueError, match="videos_max"):
            tiktok.collect({"videos": [_video()], "videos_max": "5"}, _client(handler))

    def test_bool_videos_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bool videos_max")

        with pytest.raises(ValueError, match="videos_max"):
            tiktok.collect({"videos": [_video()], "videos_max": True}, _client(handler))

    def test_exceeding_videos_max_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                "no request should be made when videos_max is exceeded"
            )

        with pytest.raises(ValueError, match="videos_max"):
            tiktok.collect(
                {
                    "videos": [
                        {"key": "one", "id": "7000000000000000001"},
                        {"key": "two", "id": "7000000000000000002"},
                    ],
                    "videos_max": 1,
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


class TestVideoSuccess:
    def test_one_video_makes_exactly_one_request_with_browser_headers(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _video_html_response()

        result = tiktok.collect({"videos": [_video()]}, _client(handler))

        assert len(requests) == 1
        req = requests[0]
        assert req.url.host == "www.tiktok.com"
        assert req.url.path == "/@i/video/7123456789012345678"
        assert req.headers["user-agent"] == tiktok._BROWSER_USER_AGENT
        assert req.headers["accept-language"] == tiktok._ACCEPT_LANGUAGE

        assert set(result) == {"tiktok.video.clip.views", "tiktok.video.clip.likes"}
        views = result["tiktok.video.clip.views"]
        assert views["value"] == 3800000
        assert views["label"] == "TT clip Views"
        assert views["attrs"] == {
            "video_id": "7123456789012345678",
            "url": "https://www.tiktok.com/@example.user/video/7123456789012345678",
            "handle": "example.user",
        }
        assert result["tiktok.video.clip.likes"]["value"] == 378600

    def test_two_videos_make_two_independent_requests(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/@i/video/7000000000000000001":
                return _video_html_response(item_id="7000000000000000001")
            return _video_html_response(item_id="7000000000000000002")

        result = tiktok.collect(
            {
                "videos": [
                    {"key": "one", "id": "7000000000000000001"},
                    {"key": "two", "id": "7000000000000000002"},
                ]
            },
            _client(handler),
        )

        assert len(requests) == 2
        assert set(result) == {
            "tiktok.video.one.views",
            "tiktok.video.one.likes",
            "tiktok.video.two.views",
            "tiktok.video.two.likes",
        }

    def test_handles_and_videos_together_make_combined_requests(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/@example.user":
                return _html_response()
            return _video_html_response()

        result = tiktok.collect(
            {"handles": [_handle()], "videos": [_video()]}, _client(handler)
        )

        assert len(requests) == 2
        assert set(result) == {
            "tiktok.user.canary.followers",
            "tiktok.user.canary.following",
            "tiktok.user.canary.videos",
            "tiktok.video.clip.views",
            "tiktok.video.clip.likes",
        }

    def test_video_url_falls_back_to_the_i_placeholder_without_an_author(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _video_html_response(author_unique_id=None)

        result = tiktok.collect({"videos": [_video()]}, _client(handler))

        assert (
            result["tiktok.video.clip.views"]["attrs"]["url"]
            == "https://www.tiktok.com/@i/video/7123456789012345678"
        )
        assert result["tiktok.video.clip.views"]["attrs"]["handle"] is None


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

    def test_zero_followers_allowed_when_opted_in(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _html_response(followerCount=0)

        result = tiktok.collect(
            {
                "handles": [
                    {
                        "key": "canary",
                        "handle": "example.user",
                        "allow_zero_followers": True,
                    }
                ]
            },
            _client(handler),
        )

        assert result["tiktok.user.canary.followers"]["value"] == 0

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


class TestVideoFailureHandling:
    def test_id_mismatch_fails_the_poll(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _video_html_response(item_id="7000000000000000000")

        with pytest.raises(ValueError, match="does not match"):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_unavailable_video_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            blob = {
                "__DEFAULT_SCOPE__": {
                    "webapp.video-detail": {
                        "statusCode": 10204,
                        "statusMsg": "status_deleted",
                    }
                }
            }
            html = (
                '<html><body><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
                f'type="application/json">{json.dumps(blob)}</script></body></html>'
            )
            return httpx.Response(200, text=html)

        with pytest.raises(ValueError, match="unavailable"):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_missing_script_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>no blob here</html>")

        with pytest.raises(ValueError, match="__UNIVERSAL_DATA_FOR_REHYDRATION__"):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_unparseable_json_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            html = (
                '<html><body><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
                'type="application/json">{not valid json</script></body></html>'
            )
            return httpx.Response(200, text=html)

        with pytest.raises(ValueError, match="not valid JSON"):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_oversized_body_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            padding = "x" * (tiktok._MAX_BODY_BYTES + 1)
            return httpx.Response(200, text=f"<html><!--{padding}--></html>")

        with pytest.raises(ValueError, match="cap"):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_missing_playcount_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            blob = {
                "__DEFAULT_SCOPE__": {
                    "webapp.video-detail": {
                        "statusCode": 0,
                        "itemInfo": {
                            "itemStruct": {
                                "id": "7123456789012345678",
                                "stats": {"diggCount": 1},
                            }
                        },
                    }
                }
            }
            html = (
                '<html><body><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
                f'type="application/json">{json.dumps(blob)}</script></body></html>'
            )
            return httpx.Response(200, text=html)

        with pytest.raises(ValueError, match="playCount is missing"):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_non_numeric_diggcount_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _video_html_response(diggCount="lots")

        with pytest.raises(ValueError, match="must be numeric"):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_bool_playcount_fails_distinctly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _video_html_response(playCount=True)

        with pytest.raises(ValueError, match="must be numeric"):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_5xx_fails_the_whole_poll(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with pytest.raises(httpx.HTTPStatusError):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_403_is_blocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        with pytest.raises(http.Blocked):
            tiktok.collect({"videos": [_video()]}, _client(handler))

    def test_429_is_rate_limited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "30"})

        with pytest.raises(http.RateLimited):
            tiktok.collect({"videos": [_video()]}, _client(handler))


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

    def test_mapping_against_the_video_fixture(self):
        html = _fixture_text("tiktok_video.html")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        result = tiktok.collect(
            {"videos": [_video(video_id="7123456789012345678")]}, _client(handler)
        )

        assert result["tiktok.video.clip.views"]["value"] == 3800000
        assert result["tiktok.video.clip.likes"]["value"] == 378600

    def test_unavailable_video_fixture_fails_distinctly(self):
        html = _fixture_text("tiktok_video_unavailable.html")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        with pytest.raises(ValueError, match="unavailable"):
            tiktok.collect({"videos": [_video()]}, _client(handler))


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
