"""Offline tests for numbers_go_up/plugins/makerworld.py.

No real MakerWorld user id was available in this environment to capture
tests/fixtures/makerworld_profile.json (the real-capture fixture the
issue's acceptance criteria calls for) -- see the PR description. The
bodies built here are synthetic, following the corrected schema quoted in
issue #26, and exist only to exercise this plugin's own parsing and
validation logic -- they are not a substitute for that fixture.
"""

import os

import httpx
import pytest

from numbers_go_up import http
from numbers_go_up.plugins import makerworld

REAL_SHAPE_BODY = {
    "downloadCount": 1067,  # inflated; must never be used
    "likeCount": 42,
    "collectionCount": 7,
    "fanCount": 13,
    "MWCount": {
        "myDesignDownloadCount": 338,
        "myInstanceDownloadCount": 250,
        "myDesignPrintCount": 19,
        "myInstancePrintCount": 12,
        "designCount": 5,
    },
    "personal": {"userLevel": {"level": 4}},
}


def _client(handler) -> httpx.Client:
    return http.build_client(transport=httpx.MockTransport(handler))


class TestUserIdValidation:
    def test_missing_user_id_is_inert_no_request_made(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made without a user_id")

        with pytest.raises(ValueError, match="user_id"):
            makerworld.collect({}, _client(handler))

    def test_empty_user_id_is_inert_no_request_made(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with an empty user_id")

        with pytest.raises(ValueError, match="user_id"):
            makerworld.collect({"user_id": ""}, _client(handler))

    def test_non_digit_user_id_is_rejected_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad user_id")

        with pytest.raises(ValueError, match="numeric"):
            makerworld.collect({"user_id": "abc123"}, _client(handler))

    def test_non_ascii_digit_user_id_is_rejected_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad user_id")

        # "\u00b2".isdigit(), "\u0661\u0662\u0663".isdigit() and
        # "\uff11\uff12\uff13".isdigit() are all True, so a bare isdigit()
        # lets them through to the profile URL.
        for bad in ["\u00b2", "\u0661\u0662\u0663", "\uff11\uff12\uff13", "12\u00b33"]:
            with pytest.raises(ValueError, match="numeric"):
                makerworld.collect({"user_id": bad}, _client(handler))

    def test_integer_user_id_is_accepted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/123456")
            return httpx.Response(200, json=REAL_SHAPE_BODY)

        result = makerworld.collect({"user_id": 123456}, _client(handler))

        assert result["makerworld.profile.likes"] == 42


class TestFieldMapping:
    def test_eight_values_map_to_the_right_fields_downloadcount_ignored(self):
        client = _client(lambda r: httpx.Response(200, json=REAL_SHAPE_BODY))

        result = makerworld.collect({"user_id": "123"}, client)

        assert result == {
            "makerworld.profile.design_downloads": 338,
            "makerworld.profile.instance_downloads": 250,
            "makerworld.profile.design_prints": 19,
            "makerworld.profile.instance_prints": 12,
            "makerworld.profile.likes": 42,
            "makerworld.profile.collections": 7,
            "makerworld.profile.followers": 13,
            "makerworld.profile.level": 4,
        }
        assert set(result) == set(makerworld.METRICS)
        # downloadCount (1067, inflated) must never appear as a value.
        assert 1067 not in result.values()


class TestMalformedResponses:
    def test_missing_mwcount_is_a_clean_error(self):
        body = {k: v for k, v in REAL_SHAPE_BODY.items() if k != "MWCount"}
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError, match="MWCount"):
            makerworld.collect({"user_id": "123"}, client)

    def test_missing_like_count_is_a_clean_error(self):
        body = {k: v for k, v in REAL_SHAPE_BODY.items() if k != "likeCount"}
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError):
            makerworld.collect({"user_id": "123"}, client)

    def test_missing_collection_count_is_a_clean_error(self):
        body = {k: v for k, v in REAL_SHAPE_BODY.items() if k != "collectionCount"}
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError):
            makerworld.collect({"user_id": "123"}, client)

    def test_missing_fan_count_is_a_clean_error(self):
        body = {k: v for k, v in REAL_SHAPE_BODY.items() if k != "fanCount"}
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError):
            makerworld.collect({"user_id": "123"}, client)

    def test_missing_user_level_is_a_clean_error(self):
        body = {**REAL_SHAPE_BODY, "personal": {}}
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError):
            makerworld.collect({"user_id": "123"}, client)

    def test_non_dict_body_is_a_clean_error(self):
        client = _client(lambda r: httpx.Response(200, json=["not", "a", "dict"]))

        with pytest.raises(ValueError):
            makerworld.collect({"user_id": "123"}, client)

    def test_html_200_page_is_a_clean_error(self):
        client = _client(lambda r: httpx.Response(200, text="<html>challenge</html>"))

        with pytest.raises(ValueError, match="not valid JSON"):
            makerworld.collect({"user_id": "123"}, client)


class TestHttpErrors:
    def test_http_403_is_not_swallowed(self):
        client = _client(lambda r: httpx.Response(403))

        with pytest.raises(httpx.HTTPStatusError):
            makerworld.collect({"user_id": "123"}, client)


@pytest.mark.live
def test_live_makerworld_profile():
    uid = os.environ.get("NGU_CANARY_MAKERWORLD_UID")
    if not uid:
        pytest.skip("NGU_CANARY_MAKERWORLD_UID not set")

    client = http.build_client()
    try:
        result = makerworld.collect({"user_id": uid}, client)
    finally:
        client.close()

    assert set(result) == set(makerworld.METRICS)
