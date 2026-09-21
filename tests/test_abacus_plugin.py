"""Offline tests for numbers_go_up/plugins/abacus.py.

tests/fixtures/abacus_info.json and abacus_not_found.json are built to
match the shapes captured live and quoted in issue #96
(`{"exists":true,"expires_in":14514616,"full_key":"K:...","is_genuine":true,
"value":6}` and the 404 `{"error":"Key not found"}` body), scrubbed to a
placeholder namespace/key -- this sandbox's network proxy blocks direct
calls to abacus.jasoncameron.dev (same caveat as github.py's and
makerworld.py's fixture tests), so these were not captured fresh in this
environment. Re-capture and replace them with a real (scrubbed) response
before relying on this plugin in production; `test_live_abacus` is the
real verification.

``abacus._last_known_values`` is process-lifetime, mutable module state
(same convention as makerworld.py's ``_warned_missing_model_ids``) -- every
test that depends on its starting state clears it first, the same way
tests/test_makerworld_plugin.py clears ``_warned_missing_model_ids``.
"""

import json
import os
import pathlib

import httpx
import pytest

from numbers_go_up import http
from numbers_go_up.plugins import abacus

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def _client(handler) -> httpx.Client:
    return http.build_client(transport=httpx.MockTransport(handler))


def _fixture(name: str):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _counter(**overrides) -> dict:
    base = {
        "key": "sfd_flash_factory",
        "namespace": "example.github.io",
        "name": "flash-finished-factory",
        "label": "Boards Flashed (factory)",
        "unit": "flashes",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_last_known_values():
    abacus._last_known_values.clear()
    yield
    abacus._last_known_values.clear()


class TestCountersValidation:
    def test_missing_counters_is_inert_no_request_made(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made without counters")

        with pytest.raises(ValueError, match="counters"):
            abacus.collect({}, _client(handler))

    def test_empty_counters_is_inert_no_request_made(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with empty counters")

        with pytest.raises(ValueError, match="counters"):
            abacus.collect({"counters": []}, _client(handler))

    def test_non_list_counters_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with non-list counters")

        with pytest.raises(ValueError, match="list"):
            abacus.collect({"counters": "not-a-list"}, _client(handler))

    def test_non_dict_counter_entry_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad entry")

        with pytest.raises(ValueError, match="mapping"):
            abacus.collect({"counters": ["not-a-dict"]}, _client(handler))

    @pytest.mark.parametrize(
        "key",
        ["Bad-Key", "bad key", "bad/key", "", None, 123, "bad.key"],
    )
    def test_invalid_key_fails_without_a_request(self, key):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad key")

        with pytest.raises(ValueError, match="key"):
            abacus.collect({"counters": [_counter(key=key)]}, _client(handler))

    def test_duplicate_key_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with duplicate keys")

        with pytest.raises(ValueError, match="duplicate"):
            abacus.collect(
                {"counters": [_counter(), _counter(name="flash-finished-app")]},
                _client(handler),
            )

    @pytest.mark.parametrize("namespace", ["", None, "has space", "has/slash", "a\tb"])
    def test_invalid_namespace_fails_without_a_request(self, namespace):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad namespace")

        with pytest.raises(ValueError, match="namespace"):
            abacus.collect(
                {"counters": [_counter(namespace=namespace)]}, _client(handler)
            )

    @pytest.mark.parametrize("name", ["", None, "has space", "has/slash", "a\nb"])
    def test_invalid_name_fails_without_a_request(self, name):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad name")

        with pytest.raises(ValueError, match="name"):
            abacus.collect({"counters": [_counter(name=name)]}, _client(handler))

    @pytest.mark.parametrize("label", ["", None, 123])
    def test_invalid_label_fails_without_a_request(self, label):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad label")

        with pytest.raises(ValueError, match="label"):
            abacus.collect({"counters": [_counter(label=label)]}, _client(handler))

    def test_non_string_unit_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad unit")

        with pytest.raises(ValueError, match="unit"):
            abacus.collect({"counters": [_counter(unit=123)]}, _client(handler))

    def test_exceeding_max_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made when exceeding max")

        with pytest.raises(ValueError, match="exceeds max"):
            abacus.collect(
                {
                    "counters": [
                        _counter(key="a", name="a"),
                        _counter(key="b", name="b"),
                    ],
                    "max": 1,
                },
                _client(handler),
            )

    def test_non_int_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad max")

        with pytest.raises(ValueError, match="max"):
            abacus.collect({"counters": [_counter()], "max": "20"}, _client(handler))

    def test_bool_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bool max")

        with pytest.raises(ValueError, match="max"):
            abacus.collect({"counters": [_counter()], "max": True}, _client(handler))

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://abacus.jasoncameron.dev",  # not https
            "https://abacus.jasoncameron.dev?x=1",  # query
            "https://abacus.jasoncameron.dev#frag",  # fragment
            "ftp://abacus.jasoncameron.dev",
            "not-a-url",
            "https://host.example/abacus",  # path prefix
            123,
        ],
    )
    def test_invalid_base_url_fails_without_a_request(self, base_url):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad base_url")

        with pytest.raises(ValueError, match="base_url"):
            abacus.collect(
                {"counters": [_counter()], "base_url": base_url}, _client(handler)
            )


class TestSuccess:
    def test_one_counter_makes_exactly_one_request(self):
        body = _fixture("abacus_info.json")
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=body)

        result = abacus.collect({"counters": [_counter()]}, _client(handler))

        assert len(requests) == 1
        assert requests[0].url.host == "abacus.jasoncameron.dev"
        assert requests[0].url.path == "/info/example.github.io/flash-finished-factory"
        assert set(result) == {"abacus.counter.sfd_flash_factory.value"}
        entry = result["abacus.counter.sfd_flash_factory.value"]
        assert entry["value"] == body["value"]
        assert entry["label"] == "Boards Flashed (factory)"
        assert entry["attrs"] == {
            "namespace": "example.github.io",
            "abacus_key": "flash-finished-factory",
            "url": (
                "https://abacus.jasoncameron.dev/info/"
                "example.github.io/flash-finished-factory"
            ),
            "expires_in": body["expires_in"],
            "unit": "flashes",
        }

    def test_two_counters_make_exactly_two_requests_correct_series(self):
        body_factory = _fixture("abacus_info.json")
        body_app = dict(body_factory, value=2)
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "flash-finished-app" in str(request.url):
                return httpx.Response(200, json=body_app)
            return httpx.Response(200, json=body_factory)

        result = abacus.collect(
            {
                "counters": [
                    _counter(),
                    _counter(
                        key="sfd_flash_app",
                        name="flash-finished-app",
                        label="Boards Flashed (app)",
                    ),
                ]
            },
            _client(handler),
        )

        assert len(requests) == 2
        assert set(result) == {
            "abacus.counter.sfd_flash_factory.value",
            "abacus.counter.sfd_flash_app.value",
        }
        assert result["abacus.counter.sfd_flash_factory.value"]["value"] == 6
        assert result["abacus.counter.sfd_flash_app.value"]["value"] == 2
        assert (
            result["abacus.counter.sfd_flash_app.value"]["label"]
            == "Boards Flashed (app)"
        )

    def test_custom_base_url_is_used(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "self-hosted.example.com"
            return httpx.Response(200, json=_fixture("abacus_info.json"))

        abacus.collect(
            {"counters": [_counter()], "base_url": "https://self-hosted.example.com"},
            _client(handler),
        )

    def test_metrics_kind_and_icon_are_fixed(self):
        meta = abacus.METRICS["abacus.counter.{key}.value"]
        assert meta["kind"] == "cumulative"
        assert meta["icon"] == "mdi:counter"


class TestNoHitPath:
    @pytest.mark.parametrize(
        "namespace,name",
        [
            ("example.github.io", "flash-finished-factory"),
            ("hit.example.com", "hit-counter"),  # "hit" as a substring, not a segment
            ("example.com", "hit"),
            ("hithithit.com", "hithithit"),
        ],
    )
    def test_no_request_path_ever_contains_hit(self, namespace, name):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_fixture("abacus_info.json"))

        abacus.collect(
            {"counters": [_counter(namespace=namespace, name=name)]},
            _client(handler),
        )

        assert requests
        for request in requests:
            assert request.url.path.startswith("/info/")
            assert not request.url.path.startswith("/hit/")

    def test_namespace_or_name_containing_a_literal_slash_is_rejected(self):
        # The one way a config value could otherwise reshape the path into
        # something containing "/hit" -- rejected outright by validation,
        # before any request, rather than relying on encoding alone.
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made")

        with pytest.raises(ValueError, match="namespace"):
            abacus.collect(
                {"counters": [_counter(namespace="example.com/hit")]},
                _client(handler),
            )

    def test_url_template_constant_uses_info_only(self):
        assert "/info/" in abacus._INFO_URL_TEMPLATE
        assert "hit" not in abacus._INFO_URL_TEMPLATE


class TestRealCaptureFixture:
    def test_mapping_against_the_info_fixture(self):
        body = _fixture("abacus_info.json")
        client = _client(lambda r: httpx.Response(200, json=body))

        result = abacus.collect({"counters": [_counter()]}, client)

        assert (
            result["abacus.counter.sfd_flash_factory.value"]["value"] == body["value"]
        )


class TestFailureHandling:
    def test_404_fails_the_poll_and_writes_no_sample(self):
        not_found = _fixture("abacus_not_found.json")
        client = _client(lambda r: httpx.Response(404, json=not_found))

        with pytest.raises(ValueError, match="not found"):
            abacus.collect({"counters": [_counter()]}, client)

        assert "sfd_flash_factory" not in abacus._last_known_values

    def test_exists_false_fails_the_poll_and_writes_no_sample(self):
        body = dict(_fixture("abacus_info.json"), exists=False)
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError, match="exists"):
            abacus.collect({"counters": [_counter()]}, client)

        assert "sfd_flash_factory" not in abacus._last_known_values

    def test_missing_value_fails_distinctly(self):
        body = dict(_fixture("abacus_info.json"))
        del body["value"]
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError, match="missing 'value'"):
            abacus.collect({"counters": [_counter()]}, client)

    def test_non_numeric_value_fails_distinctly(self):
        body = dict(_fixture("abacus_info.json"), value="six")
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError, match="must be numeric"):
            abacus.collect({"counters": [_counter()]}, client)

    def test_bool_value_fails_distinctly(self):
        body = dict(_fixture("abacus_info.json"), value=True)
        client = _client(lambda r: httpx.Response(200, json=body))

        with pytest.raises(ValueError, match="must be numeric"):
            abacus.collect({"counters": [_counter()]}, client)

    def test_nan_value_fails_distinctly(self):
        # json.loads is not RFC-strict and accepts a bare NaN literal.
        client = _client(
            lambda r: httpx.Response(
                200,
                text='{"exists": true, "value": NaN, "expires_in": 100}',
                headers={"content-type": "application/json"},
            )
        )

        with pytest.raises(ValueError, match="must be finite"):
            abacus.collect({"counters": [_counter()]}, client)

        assert "sfd_flash_factory" not in abacus._last_known_values

    def test_infinity_value_fails_distinctly(self):
        client = _client(
            lambda r: httpx.Response(
                200,
                text='{"exists": true, "value": Infinity, "expires_in": 100}',
                headers={"content-type": "application/json"},
            )
        )

        with pytest.raises(ValueError, match="must be finite"):
            abacus.collect({"counters": [_counter()]}, client)

        assert "sfd_flash_factory" not in abacus._last_known_values

    def test_unparseable_json_fails_distinctly(self):
        client = _client(lambda r: httpx.Response(200, text="<html>oops</html>"))

        with pytest.raises(ValueError, match="not valid JSON"):
            abacus.collect({"counters": [_counter()]}, client)

    def test_5xx_fails_the_whole_poll(self):
        client = _client(lambda r: httpx.Response(500))

        with pytest.raises(ValueError):
            abacus.collect({"counters": [_counter()]}, client)

    def test_plain_403_raises_blocked(self):
        client = _client(lambda r: httpx.Response(403))

        with pytest.raises(http.Blocked):
            abacus.collect({"counters": [_counter()]}, client)

    def test_429_raises_rate_limited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "5"})

        client = _client(handler)
        with pytest.raises(http.RateLimited):
            abacus.collect({"counters": [_counter()]}, client)

    def test_second_counter_failing_fails_the_whole_poll(self):
        good = _fixture("abacus_info.json")

        def handler(request: httpx.Request) -> httpx.Response:
            if "flash-finished-app" in str(request.url):
                return httpx.Response(404, json={"error": "Key not found"})
            return httpx.Response(200, json=good)

        with pytest.raises(ValueError, match="flash-finished-app"):
            abacus.collect(
                {
                    "counters": [
                        _counter(),
                        _counter(key="sfd_flash_app", name="flash-finished-app"),
                    ]
                },
                _client(handler),
            )

    def test_first_counter_success_updates_last_known_value_despite_second_failing(
        self,
    ):
        good = _fixture("abacus_info.json")

        def handler(request: httpx.Request) -> httpx.Response:
            if "flash-finished-app" in str(request.url):
                return httpx.Response(404, json={"error": "Key not found"})
            return httpx.Response(200, json=good)

        with pytest.raises(ValueError):
            abacus.collect(
                {
                    "counters": [
                        _counter(),
                        _counter(key="sfd_flash_app", name="flash-finished-app"),
                    ]
                },
                _client(handler),
            )

        assert abacus._last_known_values["sfd_flash_factory"] == good["value"]


class TestRegressionGuard:
    def test_a_value_below_the_last_stored_sample_fails_naming_both_values(self):
        first_body = dict(_fixture("abacus_info.json"), value=10)
        client_1 = _client(lambda r: httpx.Response(200, json=first_body))
        result = abacus.collect({"counters": [_counter()]}, client_1)
        assert result["abacus.counter.sfd_flash_factory.value"]["value"] == 10

        second_body = dict(_fixture("abacus_info.json"), value=3)
        client_2 = _client(lambda r: httpx.Response(200, json=second_body))

        with pytest.raises(ValueError, match=r"3.*lower.*10|10.*3"):
            abacus.collect({"counters": [_counter()]}, client_2)

    def test_an_equal_or_rising_value_is_accepted_across_polls(self):
        first_body = dict(_fixture("abacus_info.json"), value=10)
        client_1 = _client(lambda r: httpx.Response(200, json=first_body))
        abacus.collect({"counters": [_counter()]}, client_1)

        same_body = dict(_fixture("abacus_info.json"), value=10)
        client_2 = _client(lambda r: httpx.Response(200, json=same_body))
        result = abacus.collect({"counters": [_counter()]}, client_2)
        assert result["abacus.counter.sfd_flash_factory.value"]["value"] == 10

        higher_body = dict(_fixture("abacus_info.json"), value=15)
        client_3 = _client(lambda r: httpx.Response(200, json=higher_body))
        result = abacus.collect({"counters": [_counter()]}, client_3)
        assert result["abacus.counter.sfd_flash_factory.value"]["value"] == 15

    def test_regression_guard_resets_are_process_lifetime_only(self):
        # Documents the trade-off explicitly: clearing the dict (what a
        # process restart does) forgets the prior high value entirely, so
        # the exact same "counter went backwards" case that a fresh
        # process starts observing from is no longer caught.
        first_body = dict(_fixture("abacus_info.json"), value=10)
        client_1 = _client(lambda r: httpx.Response(200, json=first_body))
        abacus.collect({"counters": [_counter()]}, client_1)

        abacus._last_known_values.clear()  # simulate a restart

        lower_body = dict(_fixture("abacus_info.json"), value=1)
        client_2 = _client(lambda r: httpx.Response(200, json=lower_body))
        result = abacus.collect({"counters": [_counter()]}, client_2)
        assert result["abacus.counter.sfd_flash_factory.value"]["value"] == 1


@pytest.mark.live
def test_live_abacus():
    namespace = os.environ.get("NGU_CANARY_ABACUS_NAMESPACE")
    key = os.environ.get("NGU_CANARY_ABACUS_KEY")
    if not namespace or not key:
        pytest.skip("NGU_CANARY_ABACUS_NAMESPACE/NGU_CANARY_ABACUS_KEY not set")

    client = http.build_client()
    try:
        result = abacus.collect(
            {
                "counters": [
                    {
                        "key": "canary",
                        "namespace": namespace,
                        "name": key,
                        "label": "Canary",
                        "unit": "hits",
                    }
                ]
            },
            client,
        )
    finally:
        client.close()

    assert "abacus.counter.canary.value" in result
