"""Offline tests for numbers_go_up/plugins/github.py.

tests/fixtures/github_repo.json and github_releases.json are built to
GitHub's documented REST API schema (repos and releases endpoints), with
placeholder owner/name/ids/URLs -- see the module docstring on why these
aren't a live capture like MakerWorld's fixtures: this sandbox's network
proxy blocks direct calls to api.github.com. Re-capture and replace them
with a real (scrubbed) response before relying on this plugin in
production; the `test_live_github` canary is the real verification.
"""

import json
import os
import pathlib

import httpx
import pytest

from numbers_go_up import http
from numbers_go_up.plugins import github

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def _client(handler) -> httpx.Client:
    return http.build_client(transport=httpx.MockTransport(handler))


def _fixture(name: str):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _repo_body(**overrides) -> dict:
    body = _fixture("github_repo.json")
    body.update(overrides)
    return body


class TestReposValidation:
    def test_missing_repos_is_inert_no_request_made(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made without repos")

        with pytest.raises(ValueError, match="repos"):
            github.collect({}, _client(handler))

    def test_empty_repos_is_inert_no_request_made(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with empty repos")

        with pytest.raises(ValueError, match="repos"):
            github.collect({"repos": []}, _client(handler))

    def test_non_list_repos_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with non-list repos")

        with pytest.raises(ValueError, match="list"):
            github.collect({"repos": "owner/name"}, _client(handler))

    @pytest.mark.parametrize(
        "entry",
        [
            "no-slash",
            "owner/",
            "/name",
            "owner/name/extra",
            "owner name/repo",
            "owner/repo name",
            "a" * 40 + "/name",  # owner too long
            "owner/" + "a" * 101,  # name too long
            123,
            None,
        ],
    )
    def test_invalid_repo_entry_fails_without_a_request(self, entry):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad repo entry")

        with pytest.raises(ValueError, match="valid"):
            github.collect({"repos": [entry]}, _client(handler))

    @pytest.mark.parametrize("name", [".", ".."])
    def test_dot_repo_name_is_rejected(self, name):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a '.'/'..' repo name")

        with pytest.raises(ValueError, match="invalid repo name"):
            github.collect({"repos": [f"owner/{name}"]}, _client(handler))

    def test_case_insensitive_duplicate_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with duplicate repos")

        with pytest.raises(ValueError, match="duplicate"):
            github.collect({"repos": ["Owner/Repo", "owner/repo"]}, _client(handler))

    def test_exceeding_max_fails_without_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made when exceeding max")

        with pytest.raises(ValueError, match="exceeds max"):
            github.collect(
                {"repos": ["a/one", "a/two", "a/three"], "max": 2}, _client(handler)
            )

    def test_non_int_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bad max")

        with pytest.raises(ValueError, match="max"):
            github.collect({"repos": ["a/one"], "max": "20"}, _client(handler))

    def test_bool_max_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made with a bool max")

        with pytest.raises(ValueError, match="max"):
            github.collect({"repos": ["a/one"], "max": True}, _client(handler))

    def test_non_bool_release_downloads_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                "no request should be made with a bad release_downloads"
            )

        with pytest.raises(ValueError, match="release_downloads"):
            github.collect(
                {"repos": ["a/one"], "release_downloads": "yes"}, _client(handler)
            )


class TestSingleRepoNoReleaseDownloads:
    def test_exactly_one_request_and_four_series(self):
        body = _repo_body()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.github.com"
            assert request.url.path == "/repos/example-owner/example-repo"
            return httpx.Response(200, json=body)

        result = github.collect(
            {"repos": ["example-owner/example-repo"]}, _client(handler)
        )

        repo_id = body["id"]
        assert set(result) == {
            f"github.repo.{repo_id}.stars",
            f"github.repo.{repo_id}.forks",
            f"github.repo.{repo_id}.watchers",
            f"github.repo.{repo_id}.open_issues",
        }
        assert (
            result[f"github.repo.{repo_id}.stars"]["value"] == body["stargazers_count"]
        )
        assert result[f"github.repo.{repo_id}.forks"]["value"] == body["forks_count"]
        assert (
            result[f"github.repo.{repo_id}.open_issues"]["value"]
            == body["open_issues_count"]
        )
        assert result[f"github.repo.{repo_id}.stars"]["attrs"] == {
            "repo_id": repo_id,
            "full_name": body["full_name"],
            "url": body["html_url"],
        }
        assert (
            result[f"github.repo.{repo_id}.stars"]["label"]
            == f"GH {body['full_name']} Stars"
        )

    def test_watchers_comes_from_subscribers_count_not_watchers_count(self):
        # Fixture deliberately has watchers_count == stargazers_count (the
        # real, misleading alias) while subscribers_count differs -- proves
        # the plugin reads the right field.
        body = _repo_body(
            watchers_count=99999, subscribers_count=42, stargazers_count=1543
        )
        assert body["watchers_count"] != body["subscribers_count"]

        client = _client(lambda r: httpx.Response(200, json=body))
        result = github.collect({"repos": ["example-owner/example-repo"]}, client)

        repo_id = body["id"]
        assert result[f"github.repo.{repo_id}.watchers"]["value"] == 42


class TestReleaseDownloads:
    def test_release_downloads_false_makes_exactly_one_request(self):
        repo_body = _repo_body()
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=repo_body)

        result = github.collect(
            {"repos": ["example-owner/example-repo"], "release_downloads": False},
            _client(handler),
        )

        assert len(requests) == 1
        repo_id = repo_body["id"]
        assert f"github.repo.{repo_id}.release_downloads" not in result

    def test_release_downloads_true_sums_across_all_pages(self):
        repo_body = _repo_body()
        releases = _fixture("github_releases.json")
        page_1, page_2 = releases[:2], releases[2:]
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/releases"):
                if "page=2" in str(request.url):
                    return httpx.Response(200, json=page_2)
                return httpx.Response(
                    200,
                    json=page_1,
                    headers={
                        "Link": (
                            "<https://api.github.com/repos/example-owner/"
                            'example-repo/releases?per_page=100&page=2>; rel="next"'
                        )
                    },
                )
            return httpx.Response(200, json=repo_body)

        result = github.collect(
            {"repos": ["example-owner/example-repo"], "release_downloads": True},
            _client(handler),
        )

        release_requests = [r for r in requests if r.url.path.endswith("/releases")]
        assert len(release_requests) == 2

        expected_total = sum(
            asset["download_count"]
            for release in releases
            for asset in release["assets"]
        )
        repo_id = repo_body["id"]
        assert (
            result[f"github.repo.{repo_id}.release_downloads"]["value"]
            == expected_total
        )

    def test_no_next_link_makes_exactly_one_releases_request(self):
        repo_body = _repo_body()
        releases = _fixture("github_releases.json")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/releases"):
                return httpx.Response(200, json=releases)
            return httpx.Response(200, json=repo_body)

        github.collect(
            {"repos": ["example-owner/example-repo"], "release_downloads": True},
            _client(handler),
        )

        release_requests = [r for r in requests if r.url.path.endswith("/releases")]
        assert len(release_requests) == 1

    def test_a_listing_that_never_terminates_is_capped(self):
        repo_body = _repo_body()
        next_url = (
            "https://api.github.com/repos/example-owner/example-repo/releases?page=2"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/releases"):
                return httpx.Response(
                    200, json=[], headers={"Link": f'<{next_url}>; rel="next"'}
                )
            return httpx.Response(200, json=repo_body)

        with pytest.raises(ValueError, match="did not finish"):
            github.collect(
                {"repos": ["example-owner/example-repo"], "release_downloads": True},
                _client(handler),
            )

    def test_link_next_off_host_is_refused_not_followed(self):
        # A Link: rel="next" header pointing anywhere but api.github.com
        # must never be followed with the same headers -- that's where an
        # Authorization token would leak if a front end misbehaved.
        repo_body = _repo_body()
        releases = _fixture("github_releases.json")
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/releases"):
                return httpx.Response(
                    200,
                    json=releases[:1],
                    headers={"Link": '<https://evil.invalid/next>; rel="next"'},
                )
            return httpx.Response(200, json=repo_body)

        with pytest.raises(ValueError, match="evil.invalid"):
            github.collect(
                {"repos": ["example-owner/example-repo"], "release_downloads": True},
                _client(handler),
            )

        # Only the repo lookup and the first (legitimate) releases page --
        # never a request to the off-host URL.
        assert not any("evil.invalid" in str(r.url) for r in requests)

    def test_missing_assets_field_fails_the_whole_poll(self):
        repo_body = _repo_body()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/releases"):
                return httpx.Response(200, json=[{"id": 1, "tag_name": "v1"}])
            return httpx.Response(200, json=repo_body)

        with pytest.raises(ValueError, match="assets"):
            github.collect(
                {"repos": ["example-owner/example-repo"], "release_downloads": True},
                _client(handler),
            )


class TestRealCaptureFixture:
    def test_mapping_against_the_repo_fixture(self):
        body = _fixture("github_repo.json")
        client = _client(lambda r: httpx.Response(200, json=body))

        result = github.collect({"repos": [body["full_name"]]}, client)

        repo_id = body["id"]
        assert (
            result[f"github.repo.{repo_id}.stars"]["value"] == body["stargazers_count"]
        )
        assert (
            result[f"github.repo.{repo_id}.watchers"]["value"]
            == body["subscribers_count"]
        )


class TestFailureHandling:
    def test_404_fails_the_whole_poll_naming_the_repo(self):
        client = _client(lambda r: httpx.Response(404, json={"message": "Not Found"}))

        with pytest.raises(ValueError, match="example-owner/example-repo"):
            github.collect({"repos": ["example-owner/example-repo"]}, client)

    def test_5xx_fails_the_whole_poll(self):
        client = _client(lambda r: httpx.Response(500))

        with pytest.raises(ValueError):
            github.collect({"repos": ["example-owner/example-repo"]}, client)

    def test_non_json_body_is_a_clean_error(self):
        client = _client(lambda r: httpx.Response(200, text="<html>oops</html>"))

        with pytest.raises(ValueError, match="not valid JSON"):
            github.collect({"repos": ["example-owner/example-repo"]}, client)

    def test_second_repo_failing_fails_the_whole_poll(self):
        good = _repo_body()

        def handler(request: httpx.Request) -> httpx.Response:
            if "second-repo" in str(request.url):
                return httpx.Response(404)
            return httpx.Response(200, json=good)

        with pytest.raises(ValueError, match="second-repo"):
            github.collect(
                {"repos": ["example-owner/example-repo", "owner/second-repo"]},
                _client(handler),
            )

    def test_plain_403_still_raises_blocked(self):
        client = _client(lambda r: httpx.Response(403))

        with pytest.raises(http.Blocked):
            github.collect({"repos": ["example-owner/example-repo"]}, client)

    def test_403_with_ratelimit_remaining_zero_raises_rate_limited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "9999999999",
                },
            )

        client = _client(handler)
        with pytest.raises(http.RateLimited):
            github.collect({"repos": ["example-owner/example-repo"]}, client)


class TestRedirects:
    def test_301_redirect_for_a_renamed_repo_is_followed(self):
        body = _repo_body(full_name="example-owner/renamed-repo")
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.path == "/repos/example-owner/old-name":
                return httpx.Response(
                    301,
                    headers={
                        "Location": "https://api.github.com/repos/example-owner/renamed-repo"
                    },
                )
            return httpx.Response(200, json=body)

        result = github.collect({"repos": ["example-owner/old-name"]}, _client(handler))

        assert len(calls) == 2  # the 301, then the follow
        repo_id = body["id"]
        assert f"github.repo.{repo_id}.stars" in result


class TestToken:
    def test_no_token_env_sends_no_authorization_header(self, monkeypatch):
        monkeypatch.delenv(github._ENV_TOKEN, raising=False)
        body = _repo_body()
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=body)

        github.collect({"repos": ["example-owner/example-repo"]}, _client(handler))

        assert captured["auth"] is None

    def test_token_env_sends_bearer_authorization_to_api_github_com(self, monkeypatch):
        monkeypatch.setenv(github._ENV_TOKEN, "ghp_fake_token_value")
        body = _repo_body()
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.github.com"
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=body)

        github.collect({"repos": ["example-owner/example-repo"]}, _client(handler))

        assert captured["auth"] == "Bearer ghp_fake_token_value"


@pytest.mark.live
def test_live_github():
    repo = os.environ.get("NGU_CANARY_GITHUB_REPO")
    if not repo:
        pytest.skip("NGU_CANARY_GITHUB_REPO not set")

    client = http.build_client()
    try:
        result = github.collect({"repos": [repo]}, client)
    finally:
        client.close()

    assert any(key.endswith(".stars") for key in result)
    assert any(key.endswith(".watchers") for key in result)
