"""Offline tests for numbers_go_up/plugins/makerworld.py.

tests/fixtures/makerworld_profile.json is a real captured profile response
(captured Sep 12 2026 from a home connection with the project User-Agent,
per issue #26), with the uid, name, avatar, bio, links and designsInfo
scrubbed. The TestRealCaptureFixture class runs the plugin against that
fixture; everything else uses synthetic bodies built to the corrected
schema quoted in issue #26, which exist to exercise the plugin's own
validation and error paths.
"""

import json
import os
import pathlib

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

    def test_requests_the_bambu_api_host_not_makerworld_com(self):
        # makerworld.com is Cloudflare-challenged for non-browser TLS
        # clients (#30); the same endpoint on api.bambulab.com is not.
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.bambulab.com"
            assert request.url.path == "/v1/user-service/user/profile/123456"
            return httpx.Response(200, json=REAL_SHAPE_BODY)

        makerworld.collect({"user_id": "123456"}, _client(handler))

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


class TestRealCaptureFixture:
    """Run the plugin against the committed real-capture fixture.

    Expected values are read from the fixture JSON itself, so a re-capture
    doesn't stale these tests -- what's under test is that the plugin reads
    the right paths of a *real* response, not that it agrees with itself.
    """

    @staticmethod
    def _fixture() -> dict:
        path = pathlib.Path(__file__).parent / "fixtures" / "makerworld_profile.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_mapping_against_the_real_capture(self):
        body = self._fixture()
        client = _client(lambda r: httpx.Response(200, json=body))

        result = makerworld.collect({"user_id": "0000000000"}, client)

        mw = body["MWCount"]
        assert result == {
            "makerworld.profile.design_downloads": mw["myDesignDownloadCount"],
            "makerworld.profile.instance_downloads": mw["myInstanceDownloadCount"],
            "makerworld.profile.design_prints": mw["myDesignPrintCount"],
            "makerworld.profile.instance_prints": mw["myInstancePrintCount"],
            "makerworld.profile.likes": body["likeCount"],
            "makerworld.profile.collections": body["collectionCount"],
            "makerworld.profile.followers": body["fanCount"],
            "makerworld.profile.level": body["personal"]["userLevel"]["level"],
        }
        assert set(result) == set(makerworld.METRICS)
        # The real inflated top-level downloadCount must never be used.
        assert body["downloadCount"] not in result.values()

    def test_fixture_missing_mwcount_is_a_clean_error(self):
        body = self._fixture()
        del body["MWCount"]
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError, match="MWCount"):
            makerworld.collect({"user_id": "0000000000"}, client)

    def test_fixture_missing_likecount_is_a_clean_error(self):
        body = self._fixture()
        del body["likeCount"]
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError, match="likeCount"):
            makerworld.collect({"user_id": "0000000000"}, client)


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

        with pytest.raises(http.Blocked):
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
