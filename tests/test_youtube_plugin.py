"""Offline tests for numbers_go_up/plugins/youtube.py.

tests/fixtures/youtube_channel.json is built to the documented YouTube Data
API v3 `channels` response shape, with placeholder id/title/etags -- see the
module docstring for why this isn't a live capture (this sandbox has no
YouTube API key or channel to capture against). The `test_live_youtube`
canary is the real verification once NGU_YOUTUBE_API_KEY and
NGU_CANARY_YOUTUBE_CHANNEL_ID are set.
"""

import copy
import json
import os
import pathlib
import traceback

import httpx
import pytest

from numbers_go_up import http
from numbers_go_up.plugins import youtube

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def _client(handler) -> httpx.Client:
    return http.build_client(transport=httpx.MockTransport(handler))


def _fixture() -> dict:
    return json.loads(
        (FIXTURES_DIR / "youtube_channel.json").read_text(encoding="utf-8")
    )


def _video_fixture() -> dict:
    return json.loads((FIXTURES_DIR / "youtube_video.json").read_text(encoding="utf-8"))


def _config(**overrides) -> dict:
    config = {
        "source": "official",
        "channels": [{"key": "main", "id": "UC0000000000000000000000"}],
    }
    config.update(overrides)
    return config


class TestSourceValidation:
    def test_missing_source_fails_without_a_request(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made without a valid source")

        with pytest.raises(ValueError, match="source"):
            youtube.collect(_config(source=None), _client(handler))

    def test_livecounts_source_fails_clearly_without_a_request(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made for source=livecounts")

        with pytest.raises(ValueError, match="livecounts"):
            youtube.collect(_config(source="livecounts"), _client(handler))

    def test_livecounts_never_calls_the_official_endpoint_either(self, monkeypatch):
        # No automatic source switching exists: a rejected 'livecounts'
        # config must not silently fall back to hitting the official API.
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise AssertionError("no request should be made for source=livecounts")

        with pytest.raises(ValueError):
            youtube.collect(_config(source="livecounts"), _client(handler))

        assert calls == []


class TestChannelsValidation:
    def test_missing_channels_is_inert_no_request_made(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made without channels")

        with pytest.raises(ValueError, match="channels"):
            youtube.collect(_config(channels=None), _client(handler))

    def test_missing_api_key_fails_without_a_request(self, monkeypatch):
        monkeypatch.delenv(youtube._ENV_API_KEY, raising=False)

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made without an API key")

        with pytest.raises(ValueError, match=youtube._ENV_API_KEY):
            youtube.collect(_config(), _client(handler))

    @pytest.mark.parametrize(
        "bad_key", ["Main", "main slug", "MAIN", "", None, "main!"]
    )
    def test_invalid_key_fails_without_a_request(self, monkeypatch, bad_key):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad key")

        with pytest.raises(ValueError, match="key"):
            youtube.collect(
                _config(channels=[{"key": bad_key, "id": "UC0000000000000000000000"}]),
                _client(handler),
            )

    def test_duplicate_key_fails_without_a_request(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with duplicate keys")

        with pytest.raises(ValueError, match="duplicate"):
            youtube.collect(
                _config(
                    channels=[
                        {"key": "main", "id": "UC0000000000000000000000"},
                        {"key": "main", "id": "UC1111111111111111111111"},
                    ]
                ),
                _client(handler),
            )

    @pytest.mark.parametrize(
        "bad_id",
        [
            "UC1234",  # too short
            "uc0000000000000000000000",  # lowercase prefix -- case-sensitive
            "XX0000000000000000000000",  # wrong prefix
            "UC00000000000000000000000",  # too long
            123,
            None,
        ],
    )
    def test_invalid_channel_id_fails_without_a_request(self, monkeypatch, bad_id):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad channel id")

        with pytest.raises(ValueError, match="id"):
            youtube.collect(
                _config(channels=[{"key": "main", "id": bad_id}]), _client(handler)
            )

    def test_exceeding_max_fails_without_a_request(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made when exceeding max")

        channels = [{"key": f"ch{i}", "id": "UC" + f"{i}".zfill(22)} for i in range(3)]
        with pytest.raises(ValueError, match="exceeds max"):
            youtube.collect(_config(channels=channels, max=2), _client(handler))


class TestSingleChannel:
    def test_exactly_one_request_and_three_series(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _fixture()
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.url.host == "www.googleapis.com"
            assert request.url.params["id"] == "UC0000000000000000000000"
            assert request.url.params["key"] == "fake-key"
            return httpx.Response(200, json=body)

        result = youtube.collect(_config(), _client(handler))

        assert len(requests) == 1
        assert set(result) == {
            "youtube.channel.main.subscribers",
            "youtube.channel.main.views",
            "youtube.channel.main.videos",
        }
        stats = body["items"][0]["statistics"]
        assert result["youtube.channel.main.subscribers"]["value"] == int(
            stats["subscriberCount"]
        )
        assert result["youtube.channel.main.views"]["value"] == int(stats["viewCount"])
        assert result["youtube.channel.main.videos"]["value"] == int(
            stats["videoCount"]
        )
        assert result["youtube.channel.main.subscribers"]["attrs"] == {
            "channel_id": "UC0000000000000000000000",
            "url": "https://www.youtube.com/channel/UC0000000000000000000000",
            "source": "official",
        }
        assert (
            result["youtube.channel.main.subscribers"]["label"]
            == "YT Example Channel Subscribers"
        )

    def test_missing_title_falls_back_to_key_in_label(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _fixture()
        del body["items"][0]["snippet"]["title"]

        client = _client(lambda r: httpx.Response(200, json=body))
        result = youtube.collect(_config(), client)

        assert (
            result["youtube.channel.main.subscribers"]["label"] == "YT main Subscribers"
        )


class TestSanityGuards:
    def test_hidden_subscriber_count_fails_the_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _fixture()
        body["items"][0]["statistics"]["hiddenSubscriberCount"] = True

        client = _client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ValueError, match="hides its subscriber count"):
            youtube.collect(_config(), client)

    def test_zero_subscriber_count_fails_the_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _fixture()
        body["items"][0]["statistics"]["subscriberCount"] = "0"

        client = _client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ValueError, match="subscriberCount is 0"):
            youtube.collect(_config(), client)

    def test_missing_subscriber_count_fails_the_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _fixture()
        del body["items"][0]["statistics"]["subscriberCount"]

        client = _client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ValueError, match="is missing"):
            youtube.collect(_config(), client)

    def test_non_numeric_view_count_fails_the_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _fixture()
        body["items"][0]["statistics"]["viewCount"] = "not-a-number"

        client = _client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ValueError, match="numeric"):
            youtube.collect(_config(), client)

    def test_empty_items_fails_the_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = copy.deepcopy(_fixture())
        body["items"] = []

        client = _client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ValueError, match="not found"):
            youtube.collect(_config(), client)


class TestFailureHandling:
    def test_404_fails_the_whole_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        client = _client(lambda r: httpx.Response(404, json={"error": {"errors": []}}))

        with pytest.raises(ValueError):
            youtube.collect(_config(), client)

    def test_non_json_body_is_a_clean_error(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        client = _client(lambda r: httpx.Response(200, text="<html>oops</html>"))

        with pytest.raises(ValueError, match="not valid JSON"):
            youtube.collect(_config(), client)

    def test_second_channel_failing_fails_the_whole_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        good = _fixture()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("id") == "UC1111111111111111111111":
                return httpx.Response(404)
            return httpx.Response(200, json=good)

        with pytest.raises(ValueError):
            youtube.collect(
                _config(
                    channels=[
                        {"key": "main", "id": "UC0000000000000000000000"},
                        {"key": "second", "id": "UC1111111111111111111111"},
                    ]
                ),
                _client(handler),
            )


class TestQuotaExceeded:
    def test_403_quota_exceeded_raises_blocked_naming_the_reason(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        error_body = {
            "error": {
                "errors": [{"reason": "quotaExceeded", "message": "quota exceeded"}]
            }
        }
        client = _client(lambda r: httpx.Response(403, json=error_body))

        with pytest.raises(http.Blocked, match="quotaExceeded"):
            youtube.collect(_config(), client)


class TestKeyRedaction:
    def test_api_key_never_appears_in_a_failure_message(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "super-secret-key-value")
        client = _client(lambda r: httpx.Response(404))

        with pytest.raises(ValueError) as exc_info:
            youtube.collect(_config(), client)

        assert "super-secret-key-value" not in str(exc_info.value)

    def test_api_key_never_appears_in_a_blocked_message(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "super-secret-key-value")
        client = _client(lambda r: httpx.Response(403))

        with pytest.raises(http.Blocked) as exc_info:
            youtube.collect(_config(), client)

        assert "super-secret-key-value" not in str(exc_info.value)

    def test_api_key_never_appears_in_the_full_traceback(self, monkeypatch):
        # str(exc) alone isn't the whole story: the scheduler stores
        # traceback.format_exc(), which also renders __cause__ -- an
        # httpx.HTTPStatusError chained with `from exc` would put the raw,
        # unredacted request URL (key= included) right back in
        # plugin_runs.error even though the ValueError's own message is
        # clean. This is what a 404 (channel deleted, typo'd id, ...)
        # looks like end to end, not through the 403/429 fast path.
        monkeypatch.setenv(youtube._ENV_API_KEY, "super-secret-key-value")
        client = _client(lambda r: httpx.Response(404))

        try:
            youtube.collect(_config(), client)
        except ValueError as exc:
            # Python deletes the `as exc` binding at the end of the except
            # block, so capture what's needed before it goes out of scope.
            rendered = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            cause = exc.__cause__
        else:
            pytest.fail("expected ValueError")

        assert "super-secret-key-value" not in rendered
        assert cause is None


class TestRealCaptureFixture:
    def test_mapping_against_the_fixture(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _fixture()
        client = _client(lambda r: httpx.Response(200, json=body))

        result = youtube.collect(_config(), client)

        stats = body["items"][0]["statistics"]
        assert result["youtube.channel.main.subscribers"]["value"] == int(
            stats["subscriberCount"]
        )


class TestNeitherChannelsNorVideosConfigured:
    def test_neither_configured_fails_without_a_request(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with nothing configured")

        with pytest.raises(ValueError, match="channels or videos"):
            youtube.collect({"source": "official", "channels": None}, _client(handler))

    def test_videos_only_is_valid_with_no_channels(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _video_fixture()
        client = _client(lambda r: httpx.Response(200, json=body))

        result = youtube.collect(
            {
                "source": "official",
                "videos": [{"key": "launch", "id": "dQw4w9WgXcQ"}],
            },
            client,
        )

        assert set(result) == {
            "youtube.video.launch.views",
            "youtube.video.launch.likes",
        }


class TestVideosValidation:
    def test_missing_videos_is_inert_when_channels_configured(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _fixture()
        client = _client(lambda r: httpx.Response(200, json=body))

        result = youtube.collect(_config(videos=None), client)

        assert not any(k.startswith("youtube.video.") for k in result)

    def test_videos_not_a_list_fails_without_a_request(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with bad videos")

        with pytest.raises(ValueError, match="videos must be a list"):
            youtube.collect(_config(videos="not-a-list"), _client(handler))

    @pytest.mark.parametrize(
        "bad_key", ["Main", "main slug", "MAIN", "", None, "main!"]
    )
    def test_invalid_key_fails_without_a_request(self, monkeypatch, bad_key):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad key")

        with pytest.raises(ValueError, match="key"):
            youtube.collect(
                _config(videos=[{"key": bad_key, "id": "dQw4w9WgXcQ"}]),
                _client(handler),
            )

    def test_duplicate_key_fails_without_a_request(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with duplicate keys")

        with pytest.raises(ValueError, match="duplicate"):
            youtube.collect(
                _config(
                    videos=[
                        {"key": "launch", "id": "dQw4w9WgXcQ"},
                        {"key": "launch", "id": "aaaaaaaaaaa"},
                    ]
                ),
                _client(handler),
            )

    @pytest.mark.parametrize(
        "bad_id",
        [
            "short",  # too short
            "waytoolongvideoid",  # too long
            "bad!chars!!",  # invalid characters
            123,
            None,
        ],
    )
    def test_invalid_video_id_fails_without_a_request(self, monkeypatch, bad_id):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad video id")

        with pytest.raises(ValueError, match="id"):
            youtube.collect(
                _config(videos=[{"key": "launch", "id": bad_id}]), _client(handler)
            )

    def test_exceeding_videos_max_fails_without_a_request(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made when exceeding videos_max")

        videos = [{"key": f"v{i}", "id": f"aaaaaaaaaa{i}"} for i in range(3)]
        with pytest.raises(ValueError, match="exceeds videos_max"):
            youtube.collect(_config(videos=videos, videos_max=2), _client(handler))


class TestSingleVideo:
    def test_exactly_one_request_and_two_series(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _video_fixture()
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.url.host == "www.googleapis.com"
            assert request.url.path.endswith("/videos")
            assert request.url.params["id"] == "dQw4w9WgXcQ"
            assert request.url.params["key"] == "fake-key"
            return httpx.Response(200, json=body)

        result = youtube.collect(
            _config(channels=None, videos=[{"key": "launch", "id": "dQw4w9WgXcQ"}]),
            _client(handler),
        )

        assert len(requests) == 1
        assert set(result) == {
            "youtube.video.launch.views",
            "youtube.video.launch.likes",
        }
        stats = body["items"][0]["statistics"]
        assert result["youtube.video.launch.views"]["value"] == int(stats["viewCount"])
        assert result["youtube.video.launch.likes"]["value"] == int(stats["likeCount"])
        assert result["youtube.video.launch.views"]["attrs"] == {
            "video_id": "dQw4w9WgXcQ",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "source": "official",
        }
        assert result["youtube.video.launch.views"]["label"] == "YT Example Video Views"

    def test_missing_title_falls_back_to_key_in_label(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _video_fixture()
        del body["items"][0]["snippet"]["title"]

        client = _client(lambda r: httpx.Response(200, json=body))
        result = youtube.collect(
            _config(channels=None, videos=[{"key": "launch", "id": "dQw4w9WgXcQ"}]),
            client,
        )

        assert result["youtube.video.launch.views"]["label"] == "YT launch Views"

    def test_hidden_like_count_omits_the_likes_series(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _video_fixture()
        del body["items"][0]["statistics"]["likeCount"]

        client = _client(lambda r: httpx.Response(200, json=body))
        result = youtube.collect(
            _config(channels=None, videos=[{"key": "launch", "id": "dQw4w9WgXcQ"}]),
            client,
        )

        assert "youtube.video.launch.views" in result
        assert "youtube.video.launch.likes" not in result

    def test_zero_view_count_is_not_a_sanity_failure(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _video_fixture()
        body["items"][0]["statistics"]["viewCount"] = "0"

        client = _client(lambda r: httpx.Response(200, json=body))
        result = youtube.collect(
            _config(channels=None, videos=[{"key": "launch", "id": "dQw4w9WgXcQ"}]),
            client,
        )

        assert result["youtube.video.launch.views"]["value"] == 0

    def test_missing_view_count_fails_the_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _video_fixture()
        del body["items"][0]["statistics"]["viewCount"]

        client = _client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ValueError, match="is missing"):
            youtube.collect(
                _config(channels=None, videos=[{"key": "launch", "id": "dQw4w9WgXcQ"}]),
                client,
            )

    def test_non_numeric_like_count_fails_the_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = _video_fixture()
        body["items"][0]["statistics"]["likeCount"] = "not-a-number"

        client = _client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ValueError, match="numeric"):
            youtube.collect(
                _config(channels=None, videos=[{"key": "launch", "id": "dQw4w9WgXcQ"}]),
                client,
            )

    def test_empty_items_fails_the_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        body = copy.deepcopy(_video_fixture())
        body["items"] = []

        client = _client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ValueError, match="not found"):
            youtube.collect(
                _config(channels=None, videos=[{"key": "launch", "id": "dQw4w9WgXcQ"}]),
                client,
            )

    def test_second_video_failing_fails_the_whole_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        good = _video_fixture()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("id") == "bbbbbbbbbbb":
                return httpx.Response(404)
            return httpx.Response(200, json=good)

        with pytest.raises(ValueError):
            youtube.collect(
                _config(
                    channels=None,
                    videos=[
                        {"key": "launch", "id": "dQw4w9WgXcQ"},
                        {"key": "second", "id": "bbbbbbbbbbb"},
                    ],
                ),
                _client(handler),
            )

    def test_channel_and_video_together_in_one_poll(self, monkeypatch):
        monkeypatch.setenv(youtube._ENV_API_KEY, "fake-key")
        channel_body = _fixture()
        video_body = _video_fixture()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/videos"):
                return httpx.Response(200, json=video_body)
            return httpx.Response(200, json=channel_body)

        result = youtube.collect(
            _config(videos=[{"key": "launch", "id": "dQw4w9WgXcQ"}]),
            _client(handler),
        )

        assert "youtube.channel.main.subscribers" in result
        assert "youtube.video.launch.views" in result


@pytest.mark.live
def test_live_youtube():
    channel_id = os.environ.get("NGU_CANARY_YOUTUBE_CHANNEL_ID")
    if not channel_id:
        pytest.skip("NGU_CANARY_YOUTUBE_CHANNEL_ID not set")
    if not os.environ.get(youtube._ENV_API_KEY):
        pytest.skip(f"{youtube._ENV_API_KEY} not set")

    client = http.build_client()
    try:
        result = youtube.collect(
            {
                "source": "official",
                "channels": [{"key": "canary", "id": channel_id}],
            },
            client,
        )
    finally:
        client.close()

    assert set(result) == {
        "youtube.channel.canary.subscribers",
        "youtube.channel.canary.views",
        "youtube.channel.canary.videos",
    }
