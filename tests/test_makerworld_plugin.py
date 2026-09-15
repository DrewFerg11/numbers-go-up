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

from numbers_go_up import http, plugins
from numbers_go_up.plugins import makerworld

# The eight always-present profile metrics -- excludes the per-model
# {id} pattern entries makerworld.METRICS also carries since #55.
_PROFILE_METRIC_KEYS = {
    key for key in makerworld.METRICS if not plugins.is_pattern_key(key)
}

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
        assert set(result) == _PROFILE_METRIC_KEYS
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
        assert set(result) == _PROFILE_METRIC_KEYS
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

    assert set(result) == _PROFILE_METRIC_KEYS


def _model_hit(model_id: int, **overrides) -> dict:
    hit = {
        "id": model_id,
        "title": f"Placeholder {model_id}",
        "slug": f"placeholder-{model_id}",
        "downloadCount": 100 + model_id,
        "printCount": 10 + model_id,
        "likeCount": 5,
        "collectionCount": 2,
        "commentCount": 1,
    }
    hit.update(overrides)
    return hit


def _listing_response(total: int, hits: list) -> httpx.Response:
    return httpx.Response(200, json={"total": total, "hits": hits})


def _router(profile_body: dict, listing_pages: list[dict]):
    """A handler dispatching by path: profile requests always answer with
    profile_body; listing requests are answered from listing_pages in
    order (one entry consumed per call). Returns (handler, request_log)."""
    requests: list[httpx.Request] = []
    pages = iter(listing_pages)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/user-service/user/profile/" in str(request.url):
            return httpx.Response(200, json=profile_body)
        if "/design-service/published/" in str(request.url):
            return next(pages)
        raise AssertionError(f"unexpected request: {request.url}")

    return handler, requests


class TestModelsDisabled:
    def test_absent_models_config_makes_exactly_one_request(self):
        handler, requests = _router(REAL_SHAPE_BODY, [])
        result = makerworld.collect({"user_id": "123"}, _client(handler))

        assert len(requests) == 1
        assert set(result) == _PROFILE_METRIC_KEYS

    def test_models_enabled_false_makes_exactly_one_request(self):
        handler, requests = _router(REAL_SHAPE_BODY, [])
        result = makerworld.collect(
            {"user_id": "123", "models": {"enabled": False}}, _client(handler)
        )

        assert len(requests) == 1
        assert set(result) == _PROFILE_METRIC_KEYS


class TestModelsEnabled:
    def test_include_empty_creates_a_series_per_model_per_counter(self):
        handler, requests = _router(
            REAL_SHAPE_BODY, [_listing_response(2, [_model_hit(1), _model_hit(2)])]
        )

        result = makerworld.collect(
            {"user_id": "123", "models": {"enabled": True, "include": []}},
            _client(handler),
        )

        assert len(requests) == 2  # 1 profile + 1 listing page
        for model_id in (1, 2):
            for metric in ("downloads", "prints", "likes", "collections", "comments"):
                key = f"makerworld.model.{model_id}.{metric}"
                assert key in result
                assert result[key]["attrs"]["model_id"] == model_id
                assert result[key]["attrs"]["slug"] == f"placeholder-{model_id}"
                assert result[key]["label"].startswith(f"MW Placeholder {model_id}")

    def test_include_filters_to_only_those_models(self):
        handler, _ = _router(
            REAL_SHAPE_BODY,
            [_listing_response(2, [_model_hit(1), _model_hit(2)])],
        )

        result = makerworld.collect(
            {"user_id": "123", "models": {"enabled": True, "include": [1]}},
            _client(handler),
        )

        assert "makerworld.model.1.downloads" in result
        assert "makerworld.model.2.downloads" not in result

    def test_missing_include_id_is_skipped_and_logged_once(self, caplog):
        makerworld._warned_missing_model_ids.clear()
        handler, _ = _router(
            REAL_SHAPE_BODY, [_listing_response(1, [_model_hit(1)])] * 2
        )

        with caplog.at_level("WARNING"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True, "include": [999]}},
                _client(handler),
            )
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True, "include": [999]}},
                _client(handler),
            )

        warnings = [
            r for r in caplog.records if r.levelno >= 30 and "999" in r.getMessage()
        ]
        assert len(warnings) == 1

    def test_invalid_include_entry_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad include entry")

        with pytest.raises(ValueError, match="numeric"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True, "include": ["abc"]}},
                _client(handler),
            )

    def test_bool_include_entry_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bool include entry")

        with pytest.raises(ValueError, match="numeric"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True, "include": [True]}},
                _client(handler),
            )

    def test_max_exceeded_fails_the_poll_naming_count_and_cap(self):
        hits = [_model_hit(i) for i in range(5)]
        handler, _ = _router(REAL_SHAPE_BODY, [_listing_response(5, hits)])

        with pytest.raises(ValueError, match="5"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True, "max": 3}},
                _client(handler),
            )

    def test_paging_consumes_every_page_and_request_count_matches(self):
        page_1 = _listing_response(150, [_model_hit(i) for i in range(100)])
        page_2 = _listing_response(150, [_model_hit(i) for i in range(100, 150)])
        handler, requests = _router(REAL_SHAPE_BODY, [page_1, page_2])

        result = makerworld.collect(
            {"user_id": "123", "models": {"enabled": True, "max": 200}},
            _client(handler),
        )

        listing_requests = [
            r for r in requests if "/design-service/published/" in str(r.url)
        ]
        assert len(listing_requests) == 2
        assert "makerworld.model.0.downloads" in result
        assert "makerworld.model.149.downloads" in result

    def test_a_clamped_page_size_does_not_silently_skip_records(self):
        # If the server ever returns fewer hits than the requested limit,
        # advancing offset by the *requested* limit (rather than by the
        # hits actually returned) would silently skip records between
        # pages -- and the skipped models would then look "not returned"
        # to #54's reconciliation, deactivating their series on an
        # apparently successful poll.
        page_1 = _listing_response(150, [_model_hit(i) for i in range(60)])
        page_2 = _listing_response(150, [_model_hit(i) for i in range(60, 150)])
        handler, requests = _router(REAL_SHAPE_BODY, [page_1, page_2])

        result = makerworld.collect(
            {"user_id": "123", "models": {"enabled": True, "max": 200}},
            _client(handler),
        )

        listing_requests = [
            r for r in requests if "/design-service/published/" in str(r.url)
        ]
        assert len(listing_requests) == 2
        for model_id in range(150):
            assert f"makerworld.model.{model_id}.downloads" in result

    def test_non_int_total_is_a_clean_error(self):
        handler, _ = _router(
            REAL_SHAPE_BODY, [httpx.Response(200, json={"total": "150", "hits": []})]
        )

        with pytest.raises(ValueError, match="total"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True}}, _client(handler)
            )

    def test_a_listing_that_never_terminates_is_capped(self):
        # A server bug or shape change that makes `total` permanently
        # exceed cumulative offset must fail loudly after a bounded number
        # of pages, not hammer the host and grow memory forever.
        def handler(request: httpx.Request) -> httpx.Response:
            if "/user-service/user/profile/" in str(request.url):
                return httpx.Response(200, json=REAL_SHAPE_BODY)
            return httpx.Response(200, json={"total": 10**9, "hits": [_model_hit(1)]})

        with pytest.raises(ValueError, match="did not finish"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True, "max": 10**9}},
                _client(handler),
            )

    def test_non_int_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad models.max")

        with pytest.raises(ValueError, match="max"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True, "max": "50"}},
                _client(handler),
            )

    def test_bool_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bool models.max")

        with pytest.raises(ValueError, match="max"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True, "max": True}},
                _client(handler),
            )

    def test_duplicate_include_entries_count_once_toward_max(self):
        handler, _ = _router(REAL_SHAPE_BODY, [_listing_response(1, [_model_hit(1)])])

        # Without exercising the plugin: max=1 with a de-duplicated
        # include of a single id must not exceed the cap even though the
        # id is listed twice.
        result = makerworld.collect(
            {
                "user_id": "123",
                "models": {"enabled": True, "include": [1, 1], "max": 1},
            },
            _client(handler),
        )

        assert "makerworld.model.1.downloads" in result

    def test_non_mapping_models_config_disables_the_feature_with_a_warning(
        self, caplog
    ):
        handler, requests = _router(REAL_SHAPE_BODY, [])

        with caplog.at_level("WARNING"):
            result = makerworld.collect(
                {"user_id": "123", "models": True}, _client(handler)
            )

        assert len(requests) == 1  # profile only -- no listing request
        assert set(result) == _PROFILE_METRIC_KEYS
        assert "models" in caplog.text.lower()

    def test_listing_failure_fails_the_whole_poll(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/user-service/user/profile/" in str(request.url):
                return httpx.Response(200, json=REAL_SHAPE_BODY)
            return httpx.Response(200, text="<html>not json</html>")

        with pytest.raises(ValueError, match="not valid JSON"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True}}, _client(handler)
            )

    def test_listing_missing_hits_field_fails_the_whole_poll(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/user-service/user/profile/" in str(request.url):
                return httpx.Response(200, json=REAL_SHAPE_BODY)
            return httpx.Response(200, json={"total": 1})

        with pytest.raises(ValueError, match="missing expected field"):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True}}, _client(handler)
            )

    def test_listing_403_is_not_swallowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/user-service/user/profile/" in str(request.url):
                return httpx.Response(200, json=REAL_SHAPE_BODY)
            return httpx.Response(403)

        with pytest.raises(http.Blocked):
            makerworld.collect(
                {"user_id": "123", "models": {"enabled": True}}, _client(handler)
            )


class TestModelsRealCaptureFixture:
    def test_scrubbed_capture_maps_correctly(self):
        path = pathlib.Path(__file__).parent / "fixtures" / "makerworld_models.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        handler, _ = _router(REAL_SHAPE_BODY, [httpx.Response(200, json=body)])

        result = makerworld.collect(
            {"user_id": "123", "models": {"enabled": True}}, _client(handler)
        )

        for hit in body["hits"]:
            model_id = hit["id"]
            assert (
                result[f"makerworld.model.{model_id}.downloads"]["value"]
                == (hit["downloadCount"])
            )
            assert (
                result[f"makerworld.model.{model_id}.likes"]["value"]
                == (hit["likeCount"])
            )


@pytest.mark.live
def test_live_makerworld_models():
    uid = os.environ.get("NGU_CANARY_MAKERWORLD_UID")
    if not uid:
        pytest.skip("NGU_CANARY_MAKERWORLD_UID not set")

    client = http.build_client()
    try:
        result = makerworld.collect(
            {"user_id": uid, "models": {"enabled": True}}, client
        )
    finally:
        client.close()

    # Don't require at least one model: a canary account with zero
    # published models is a valid state, not a plugin failure. The
    # profile metrics must still be present, and any per-model key that
    # does show up must have the right shape.
    assert set(result) >= _PROFILE_METRIC_KEYS
    for key, value in result.items():
        if key.startswith("makerworld.model."):
            assert isinstance(value, dict)
            assert isinstance(value["value"], int | float)
            assert isinstance(value["attrs"]["model_id"], int)
