import json
import logging
import time

import httpx
import pytest

from numbers_go_up import http, migrate, milestones, plugins, storage
from numbers_go_up.config import ConfigError

SENTINEL_URL = "https://ha.local.invalid/api/webhook/sentinel-secret-do-not-leak-xyz"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(path)
    return path


def _seed_series(
    db_path,
    metric_key,
    plugin_name=None,
    kind="cumulative",
    label="Label",
    unit="widgets",
    value=None,
    now=1000,
    attrs=None,
):
    plugin_name = plugin_name or metric_key.split(".", 1)[0]
    storage.get_or_create_series(
        db_path, metric_key, plugin_name, kind, label, unit, None, now, attrs=attrs
    )
    row = storage.get_series_by_key(db_path, metric_key)
    if value is not None:
        storage.record_sample(db_path, row["id"], now, value, 86400)
    return row["id"]


def _rule(metric, every=None, at=None):
    return {"metric": metric, "every": every, "at": sorted(at) if at else []}


class _QueueTransport:
    """A MockTransport-shaped double that pops one queued response per
    request (default: always 200), and records every posted payload."""

    def __init__(self, responses=None):
        self.responses = list(responses) if responses else []
        self.sent: list[dict] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.sent.append(json.loads(request.content))
        status = self.responses.pop(0) if self.responses else 200
        if status == "raise":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(status, json={"ok": status < 300})


def _evaluator(db_path, rules, transport=None, webhook_url=SENTINEL_URL):
    transport = transport or _QueueTransport()
    client = http.build_client(transport=httpx.MockTransport(transport.handler))
    evaluator = milestones.MilestoneEvaluator(rules, webhook_url, db_path, client)
    return evaluator, transport


def _poll(evaluator, plugin_name, key, previous, current, status="ok"):
    """Simulate one finished poll returning a single key."""
    evaluator.on_poll_finished(
        plugin_name,
        status,
        frozenset({key}),
        {key: previous},
        {key: current},
    )


# --- config validation ------------------------------------------------


class TestValidateMilestonesConfig:
    def test_missing_block_is_off(self):
        assert milestones.validate_milestones_config(None, {}, []) is None

    def test_non_mapping_raises(self):
        with pytest.raises(ConfigError):
            milestones.validate_milestones_config("nope", {}, [])

    def test_missing_webhook_url_env_raises(self):
        with pytest.raises(ConfigError, match="webhook_url_env"):
            milestones.validate_milestones_config({"rules": []}, {}, [])

    def test_env_var_not_set_raises(self):
        raw = {
            "webhook_url_env": "NGU_MILESTONE_WEBHOOK_URL",
            "rules": [{"metric": "acme.x", "every": 500}],
        }
        with pytest.raises(ConfigError, match="NGU_MILESTONE_WEBHOOK_URL"):
            milestones.validate_milestones_config(raw, {}, [])

    def test_missing_rules_raises(self):
        raw = {"webhook_url_env": "X"}
        with pytest.raises(ConfigError, match="rules"):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    def test_empty_rules_raises(self):
        raw = {"webhook_url_env": "X", "rules": []}
        with pytest.raises(ConfigError, match="rules"):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    def test_rule_not_a_mapping_raises(self):
        raw = {"webhook_url_env": "X", "rules": ["not-a-dict"]}
        with pytest.raises(ConfigError):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    def test_rule_missing_metric_raises(self):
        raw = {"webhook_url_env": "X", "rules": [{"every": 500}]}
        with pytest.raises(ConfigError, match="metric"):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    def test_rule_with_neither_every_nor_at_raises(self):
        raw = {"webhook_url_env": "X", "rules": [{"metric": "acme.x"}]}
        with pytest.raises(ConfigError, match="every"):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    @pytest.mark.parametrize("every", [0, -500, "500", True])
    def test_rule_with_bad_every_raises(self, every):
        raw = {"webhook_url_env": "X", "rules": [{"metric": "acme.x", "every": every}]}
        with pytest.raises(ConfigError, match="every"):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    def test_rule_with_empty_at_raises(self):
        raw = {"webhook_url_env": "X", "rules": [{"metric": "acme.x", "at": []}]}
        with pytest.raises(ConfigError, match="at"):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    @pytest.mark.parametrize("at", [[0], [-5], ["1000"], [1000, True]])
    def test_rule_with_bad_at_values_raises(self, at):
        raw = {"webhook_url_env": "X", "rules": [{"metric": "acme.x", "at": at}]}
        with pytest.raises(ConfigError, match="at"):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    def test_pattern_metric_matching_no_plugin_raises(self):
        raw = {
            "webhook_url_env": "X",
            "rules": [{"metric": "makerworld.model.{id}.downloads", "every": 100}],
        }
        with pytest.raises(ConfigError, match="matches no enabled plugin"):
            milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])

    def test_pattern_metric_matching_a_plugin_is_valid(self):
        plugin = plugins.LoadedPlugin(
            name="makerworld",
            module=object(),
            metrics={
                "makerworld.model.{id}.downloads": {"kind": "cumulative", "unit": "d"}
            },
            interval_seconds=1800,
            config={},
            source="built-in",
        )
        raw = {
            "webhook_url_env": "X",
            "rules": [{"metric": "makerworld.model.{id}.downloads", "every": 100}],
        }
        result = milestones.validate_milestones_config(
            raw, {"X": SENTINEL_URL}, [plugin]
        )
        assert result["rules"] == [
            {"metric": "makerworld.model.{id}.downloads", "every": 100, "at": []}
        ]

    def test_exact_metric_need_not_exist_yet(self):
        raw = {
            "webhook_url_env": "X",
            "rules": [{"metric": "acme.not_born_yet", "every": 10}],
        }
        result = milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])
        assert result["rules"][0]["metric"] == "acme.not_born_yet"

    def test_every_and_at_combined_rule_is_valid(self):
        raw = {
            "webhook_url_env": "X",
            "rules": [{"metric": "acme.x", "every": 500, "at": [250, 750]}],
        }
        result = milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])
        assert result["rules"][0]["every"] == 500
        assert result["rules"][0]["at"] == [250, 750]

    def test_webhook_url_env_setting_never_returned_unmasked_but_is_usable(self):
        raw = {"webhook_url_env": "X", "rules": [{"metric": "acme.x", "every": 500}]}
        result = milestones.validate_milestones_config(raw, {"X": SENTINEL_URL}, [])
        assert result["webhook_url"] == SENTINEL_URL


class TestBuildEvaluator:
    def test_no_milestones_block_returns_noop(self, tmp_path):
        evaluator = milestones.build_evaluator(
            {"storage": {"path": str(tmp_path / "x.db")}}, [], http_client=None
        )
        assert isinstance(evaluator, milestones.NoopEvaluator)
        assert evaluator.status["enabled"] is False

    def test_noop_hook_is_inert(self):
        evaluator = milestones.NoopEvaluator()
        evaluator.on_poll_finished("acme", "ok", frozenset({"acme.x"}), {}, {})

    def test_present_but_invalid_block_raises_config_error(self, tmp_path):
        config = {
            "storage": {"path": str(tmp_path / "x.db")},
            "milestones": {"webhook_url_env": "NGU_MILESTONE_WEBHOOK_URL"},
        }
        with pytest.raises(ConfigError):
            milestones.build_evaluator(config, [], http_client=None, env={})

    def test_valid_block_builds_a_real_evaluator(self, tmp_path):
        db = tmp_path / "x.db"
        migrate.run_migrations(db)
        config = {
            "storage": {"path": str(db)},
            "milestones": {
                "webhook_url_env": "NGU_MILESTONE_WEBHOOK_URL",
                "rules": [{"metric": "acme.x", "every": 500}],
            },
        }
        client = http.build_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        evaluator = milestones.build_evaluator(
            config,
            [],
            http_client=client,
            env={"NGU_MILESTONE_WEBHOOK_URL": SENTINEL_URL},
        )
        assert isinstance(evaluator, milestones.MilestoneEvaluator)
        assert evaluator.status["enabled"] is True


# --- crossing -----------------------------------------------------------


class TestCrossing:
    def test_single_crossing_fires(self, db_path):
        _seed_series(db_path, "acme.x", value=490)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        # Establish the marker silently first (simulates the rule already
        # having been in place with nothing crossed yet).
        _poll(evaluator, "acme", "acme.x", previous=None, current=490)
        assert transport.sent == []

        _poll(evaluator, "acme", "acme.x", previous=490, current=503)

        assert len(transport.sent) == 1
        assert transport.sent[0]["threshold"] == 500
        assert transport.sent[0]["value"] == 503
        assert transport.sent[0]["previous"] == 490

    def test_multi_threshold_burst_fires_once_for_the_highest(self, db_path):
        _seed_series(db_path, "acme.x", value=100)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)

        _poll(evaluator, "acme", "acme.x", previous=480, current=1020)

        assert len(transport.sent) == 1
        assert transport.sent[0]["threshold"] == 1000

    def test_exact_hit_fires(self, db_path):
        _seed_series(db_path, "acme.x", value=100)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)

        _poll(evaluator, "acme", "acme.x", previous=499, current=500)

        assert len(transport.sent) == 1
        assert transport.sent[0]["threshold"] == 500

    def test_previous_equal_to_threshold_does_not_fire(self, db_path):
        _seed_series(db_path, "acme.x", value=500)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        _poll(evaluator, "acme", "acme.x", previous=None, current=500)

        _poll(evaluator, "acme", "acme.x", previous=500, current=500)

        assert transport.sent == []

    def test_every_and_at_combined_rule_unions_thresholds_fire_once(self, db_path):
        _seed_series(db_path, "acme.x", value=100)
        rule = _rule("acme.x", every=500, at=[250, 750])
        evaluator, transport = _evaluator(db_path, [rule])
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)

        # crosses 250 ("at") and nothing from "every" yet
        _poll(evaluator, "acme", "acme.x", previous=100, current=300)
        assert [s["threshold"] for s in transport.sent] == [250]

        # crosses 500 ("every") and 750 ("at") in one jump -- highest wins
        _poll(evaluator, "acme", "acme.x", previous=300, current=800)
        assert [s["threshold"] for s in transport.sent] == [250, 750]


# --- marker / no history spam -------------------------------------------


class TestMarker:
    def test_gauge_dips_and_recrosses_without_refiring(self, db_path):
        _seed_series(db_path, "acme.x", value=100)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)
        _poll(evaluator, "acme", "acme.x", previous=100, current=1200)
        assert [s["threshold"] for s in transport.sent] == [1000]

        # dips below 1000, then climbs back up to 1050 -- must not refire 1000
        _poll(evaluator, "acme", "acme.x", previous=1200, current=600)
        _poll(evaluator, "acme", "acme.x", previous=600, current=1050)

        assert [s["threshold"] for s in transport.sent] == [1000]

    def test_restart_with_same_db_does_not_refire(self, db_path):
        rule = _rule("acme.x", every=500)
        _seed_series(db_path, "acme.x", value=100)
        evaluator, transport = _evaluator(db_path, [rule])
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)
        _poll(evaluator, "acme", "acme.x", previous=100, current=550)
        assert [s["threshold"] for s in transport.sent] == [500]

        # a brand new process: a fresh evaluator, same db
        evaluator2, transport2 = _evaluator(db_path, [rule])
        _poll(evaluator2, "acme", "acme.x", previous=550, current=560)

        assert transport2.sent == []

    def test_rule_added_to_existing_series_initialises_silently(self, db_path):
        # series already at 3412 with no milestone history at all
        _seed_series(db_path, "acme.x", value=3412)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=100)])

        _poll(evaluator, "acme", "acme.x", previous=3412, current=3412)

        assert transport.sent == []
        marker = storage.get_state(db_path, "milestone:acme.x")
        assert float(marker) == 3400

        # next real crossing still fires correctly afterwards
        _poll(evaluator, "acme", "acme.x", previous=3412, current=3510)
        assert [s["threshold"] for s in transport.sent] == [3500]

    def test_new_series_first_sample_never_fires(self, db_path):
        _seed_series(db_path, "acme.x", value=600)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])

        _poll(evaluator, "acme", "acme.x", previous=None, current=600)

        assert transport.sent == []
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 500

    def test_startup_never_fires(self, db_path):
        # No poll has ever run -- on_poll_finished is simply never called.
        # Nothing to assert beyond "nothing crashes and nothing is sent".
        _seed_series(db_path, "acme.x", value=600)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        assert transport.sent == []
        assert storage.get_state(db_path, "milestone:acme.x") is None


# --- pattern rules --------------------------------------------------------


class TestPatternRules:
    def test_pattern_rule_applies_to_every_matching_series(self, db_path):
        rule = _rule("acme.model.{id}.downloads", every=100)
        evaluator, transport = _evaluator(db_path, [rule])

        _seed_series(db_path, "acme.model.a.downloads", value=50)
        _seed_series(db_path, "acme.model.b.downloads", value=50)
        evaluator.on_poll_finished(
            "acme",
            "ok",
            frozenset({"acme.model.a.downloads", "acme.model.b.downloads"}),
            {"acme.model.a.downloads": None, "acme.model.b.downloads": None},
            {"acme.model.a.downloads": 50, "acme.model.b.downloads": 50},
        )
        assert transport.sent == []

        evaluator.on_poll_finished(
            "acme",
            "ok",
            frozenset({"acme.model.a.downloads", "acme.model.b.downloads"}),
            {"acme.model.a.downloads": 50, "acme.model.b.downloads": 50},
            {"acme.model.a.downloads": 120, "acme.model.b.downloads": 105},
        )

        thresholds = sorted((s["metric"], s["threshold"]) for s in transport.sent)
        assert thresholds == [
            ("acme.model.a.downloads", 100),
            ("acme.model.b.downloads", 100),
        ]

    def test_pattern_rule_covers_a_series_appearing_after_startup(self, db_path):
        rule = _rule("acme.model.{id}.downloads", every=100)
        evaluator, transport = _evaluator(db_path, [rule])

        _seed_series(db_path, "acme.model.a.downloads", value=50)
        evaluator.on_poll_finished(
            "acme",
            "ok",
            frozenset({"acme.model.a.downloads"}),
            {"acme.model.a.downloads": None},
            {"acme.model.a.downloads": 50},
        )

        # a new model appears in a later poll
        _seed_series(db_path, "acme.model.new.downloads", value=None, now=2000)
        evaluator.on_poll_finished(
            "acme",
            "ok",
            frozenset({"acme.model.a.downloads", "acme.model.new.downloads"}),
            {"acme.model.a.downloads": 50, "acme.model.new.downloads": None},
            {"acme.model.a.downloads": 50, "acme.model.new.downloads": 40},
        )
        assert transport.sent == []  # first sample of the new series

        evaluator.on_poll_finished(
            "acme",
            "ok",
            frozenset({"acme.model.new.downloads"}),
            {"acme.model.new.downloads": 40},
            {"acme.model.new.downloads": 110},
        )
        assert [s["metric"] for s in transport.sent] == ["acme.model.new.downloads"]


# --- delivery -------------------------------------------------------------


class TestDelivery:
    def test_5xx_leaves_pending_and_retries_on_next_successful_poll(self, db_path):
        transport = _QueueTransport(responses=[500])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)

        _poll(evaluator, "acme", "acme.x", previous=100, current=550)
        assert len(transport.sent) == 1  # attempted, failed
        # A crossing counts as "fired" the moment it's queued: the marker
        # is stamped at queue time, before delivery even succeeds -- a
        # retry of 500 still finds it pending, just not because the
        # marker withheld it.
        assert storage.get_state(db_path, "milestone_pending:acme.x") is not None
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 500

        # next successful poll of the same plugin retries it, this time ok
        transport.responses.append(200)
        _poll(evaluator, "acme", "acme.x", previous=550, current=551)

        assert len(transport.sent) == 2
        assert storage.get_state(db_path, "milestone_pending:acme.x") is None
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 500

    def test_2xx_clears_pending_and_advances_marker_atomically(self, db_path):
        _seed_series(db_path, "acme.x", value=100)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)

        _poll(evaluator, "acme", "acme.x", previous=100, current=550)

        assert storage.get_state(db_path, "milestone_pending:acme.x") is None
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 500

    def test_pending_survives_restart(self, db_path):
        rule = _rule("acme.x", every=500)
        transport = _QueueTransport(responses=[500])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(db_path, [rule], transport=transport)
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)
        _poll(evaluator, "acme", "acme.x", previous=100, current=550)
        assert storage.get_state(db_path, "milestone_pending:acme.x") is not None

        # a fresh process, same db, retries what was pending
        transport2 = _QueueTransport(responses=[200])
        evaluator2, _ = _evaluator(db_path, [rule], transport=transport2)
        _poll(evaluator2, "acme", "acme.x", previous=551, current=552)

        assert len(transport2.sent) == 1
        assert storage.get_state(db_path, "milestone_pending:acme.x") is None
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 500

    def test_dropped_after_24h_of_failed_retries_with_one_warning(
        self, db_path, caplog
    ):
        _seed_series(db_path, "acme.x", value=100)
        # Seed a pending record directly, as if queued more than 24h ago.
        old_record = {
            "threshold": 500,
            "payload": {"metric": "acme.x", "threshold": 500},
            "queued_at": time.time() - (25 * 3600),
        }
        storage.set_state(db_path, "milestone_pending:acme.x", json.dumps(old_record))
        storage.set_state(db_path, "milestone:acme.x", "0.0")

        transport = _QueueTransport(responses=[500])
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )

        with caplog.at_level(logging.WARNING, logger="numbers_go_up.milestones"):
            _poll(evaluator, "acme", "acme.x", previous=100, current=101)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("dropping pending delivery" in r.getMessage() for r in warnings)
        assert storage.get_state(db_path, "milestone_pending:acme.x") is None
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 500

    def test_a_newer_higher_crossing_replaces_an_older_pending_one(self, db_path):
        transport = _QueueTransport(responses=[500, 500])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)

        _poll(evaluator, "acme", "acme.x", previous=100, current=550)
        first_pending = json.loads(
            storage.get_state(db_path, "milestone_pending:acme.x")
        )
        assert first_pending["threshold"] == 500

        _poll(evaluator, "acme", "acme.x", previous=550, current=1200)
        second_pending = json.loads(
            storage.get_state(db_path, "milestone_pending:acme.x")
        )
        assert second_pending["threshold"] == 1000
        # The marker advances to cover *both* the discarded 500 crossing
        # (so a later dip-and-recross of it stays silent) and the new
        # 1000 one (so a later regression at delivery time -- e.g. this
        # 1000 record itself later displaced and its stale delivery
        # succeeding -- can't uncover it either) -- all in the same
        # transaction as the overwrite, not only when/if 1000 is
        # eventually delivered.
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 1000

    def test_discarded_pending_threshold_never_refires_after_a_dip(self, db_path):
        # Same setup as the replace-on-overwrite test above, but goes one
        # step further: once 500's pending record is superseded by 1000
        # (which then delivers cleanly, clearing all pending state), a
        # later dip back below 500 and re-cross must stay silent -- the
        # discarded milestone was already "won" by the higher one.
        transport = _QueueTransport(responses=[500, 500, 200])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)
        _poll(evaluator, "acme", "acme.x", previous=100, current=550)
        _poll(evaluator, "acme", "acme.x", previous=550, current=1200)
        # 1000 is still pending (failed); retry it to a clean slate so the
        # dip/re-cross below is the only thing that could send anything.
        _poll(evaluator, "acme", "acme.x", previous=1200, current=1201)
        assert storage.get_state(db_path, "milestone_pending:acme.x") is None
        transport.sent.clear()

        _poll(evaluator, "acme", "acme.x", previous=1201, current=480)
        _poll(evaluator, "acme", "acme.x", previous=480, current=560)

        assert transport.sent == []

    def test_marker_never_regresses_when_a_stale_pending_delivery_succeeds(
        self, db_path
    ):
        # A displaced pending record (queued while the marker was lower,
        # then overtaken by a higher crossing that advanced the marker)
        # must not be able to stamp the marker back down to its own,
        # now-stale threshold when it's finally retried successfully.
        _seed_series(db_path, "acme.x", value=100)
        storage.set_state(db_path, "milestone:acme.x", "2500.0")
        stale_record = {
            "threshold": 1000,
            "payload": {"metric": "acme.x", "threshold": 1000},
            "queued_at": time.time(),
        }
        storage.set_state(db_path, "milestone_pending:acme.x", json.dumps(stale_record))
        transport = _QueueTransport(responses=[200])
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )

        _poll(evaluator, "acme", "acme.x", previous=2500, current=2501)

        assert float(storage.get_state(db_path, "milestone:acme.x")) == 2500
        assert storage.get_state(db_path, "milestone_pending:acme.x") is None

    def test_marker_never_regresses_when_a_stale_pending_delivery_ages_out(
        self, db_path
    ):
        # Same hazard as above, via the 24h-drop path instead of a 2xx.
        _seed_series(db_path, "acme.x", value=100)
        storage.set_state(db_path, "milestone:acme.x", "2500.0")
        stale_record = {
            "threshold": 1000,
            "payload": {"metric": "acme.x", "threshold": 1000},
            "queued_at": time.time() - (25 * 3600),
        }
        storage.set_state(db_path, "milestone_pending:acme.x", json.dumps(stale_record))
        transport = _QueueTransport(responses=[500])
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )

        _poll(evaluator, "acme", "acme.x", previous=2500, current=2501)

        assert float(storage.get_state(db_path, "milestone:acme.x")) == 2500
        assert storage.get_state(db_path, "milestone_pending:acme.x") is None

    def test_a_still_pending_higher_milestone_survives_a_later_lower_crossing(
        self, db_path
    ):
        # A still-undelivered *higher* pending record must not be silently
        # replaced (and lost) by a *lower* fresh crossing arriving later
        # while the webhook is still down -- the marker is stamped at
        # queue time on every fresh crossing, not only when overwriting an
        # existing pending row, so a lower crossing finds it already
        # covered and never reaches the overwrite at all.
        transport = _QueueTransport(responses=[500, 500, 500])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )
        _poll(evaluator, "acme", "acme.x", previous=None, current=100)

        # Burst straight to 2600: fires 2500 (highest wins), POST fails.
        _poll(evaluator, "acme", "acme.x", previous=100, current=2600)
        pending = json.loads(storage.get_state(db_path, "milestone_pending:acme.x"))
        assert pending["threshold"] == 2500
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 2500

        # Dip, then recross 1500 -- already covered by the marker, so this
        # must not touch the still-pending 2500 record at all.
        _poll(evaluator, "acme", "acme.x", previous=2600, current=900)
        _poll(evaluator, "acme", "acme.x", previous=900, current=1600)

        still_pending = json.loads(
            storage.get_state(db_path, "milestone_pending:acme.x")
        )
        assert still_pending["threshold"] == 2500
        assert float(storage.get_state(db_path, "milestone:acme.x")) == 2500

    def test_isolation_one_series_error_does_not_block_another(self, db_path):
        _seed_series(db_path, "acme.a", value=100)
        _seed_series(db_path, "acme.b", value=100)
        rules = [_rule("acme.a", every=500), _rule("acme.b", every=500)]
        evaluator, transport = _evaluator(db_path, rules)
        evaluator.on_poll_finished(
            "acme",
            "ok",
            frozenset({"acme.a", "acme.b"}),
            {"acme.a": None, "acme.b": None},
            {"acme.a": 100, "acme.b": 100},
        )

        # Corrupt acme.a's marker so evaluating it raises; acme.b must still fire.
        storage.set_state(db_path, "milestone:acme.a", "not-a-number")

        evaluator.on_poll_finished(
            "acme",
            "ok",
            frozenset({"acme.a", "acme.b"}),
            {"acme.a": 100, "acme.b": 100},
            {"acme.a": 550, "acme.b": 550},
        )

        assert [s["metric"] for s in transport.sent] == ["acme.b"]


# --- payload shape ----------------------------------------------------


class TestPayload:
    def test_payload_shape_and_thousands_separators(self, db_path):
        _seed_series(
            db_path,
            "makerworld.profile.design_downloads",
            plugin_name="makerworld",
            label="MW Design Downloads",
            unit="downloads",
            value=100,
            attrs={"url": "https://makerworld.com/en/@someone"},
        )
        evaluator, transport = _evaluator(
            db_path, [_rule("makerworld.profile.design_downloads", every=500)]
        )
        _poll(evaluator, "makerworld", "makerworld.profile.design_downloads", None, 100)

        _poll(evaluator, "makerworld", "makerworld.profile.design_downloads", 497, 503)

        payload = transport.sent[0]
        assert payload["message"] == "MW Design Downloads passed 500 (now 503)"
        assert payload["metric"] == "makerworld.profile.design_downloads"
        assert payload["label"] == "MW Design Downloads"
        assert payload["unit"] == "downloads"
        assert payload["threshold"] == 500
        assert payload["value"] == 503
        assert payload["previous"] == 497
        assert payload["url"] == "https://makerworld.com/en/@someone"
        assert payload["timestamp"].endswith("Z")

    def test_thousands_separators_only_in_message(self, db_path):
        _seed_series(db_path, "acme.x", value=100)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        _poll(evaluator, "acme", "acme.x", None, 100)

        _poll(evaluator, "acme", "acme.x", 999_499, 1_000_000)

        payload = transport.sent[0]
        assert "1,000,000" in payload["message"]
        assert payload["threshold"] == 1_000_000
        assert payload["value"] == 1_000_000

    def test_url_is_none_when_attrs_has_no_url(self, db_path):
        _seed_series(db_path, "acme.x", value=100)
        evaluator, transport = _evaluator(db_path, [_rule("acme.x", every=500)])
        _poll(evaluator, "acme", "acme.x", None, 100)

        _poll(evaluator, "acme", "acme.x", 100, 550)

        assert transport.sent[0]["url"] is None


# --- webhook URL never leaks ----------------------------------------------


class TestWebhookUrlNeverLeaks:
    def test_url_never_in_status(self, db_path):
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(db_path, [_rule("acme.x", every=500)])
        _poll(evaluator, "acme", "acme.x", None, 100)
        _poll(evaluator, "acme", "acme.x", 100, 550)

        assert SENTINEL_URL not in json.dumps(evaluator.status)

    def test_url_never_leaks_on_a_403_through_the_shared_client(self, db_path, caplog):
        # A 403 through the real shared client (http.build_client, not a
        # bare MockTransport response) is turned into http.Blocked, whose
        # own message embeds the full request URL -- the one failure mode
        # the ConnectError/non-2xx-status tests above don't exercise.
        transport = _QueueTransport(responses=[403])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )
        _poll(evaluator, "acme", "acme.x", None, 100)

        with caplog.at_level(logging.WARNING, logger="numbers_go_up.milestones"):
            _poll(evaluator, "acme", "acme.x", 100, 550)

        assert SENTINEL_URL not in json.dumps(evaluator.status)
        last_error = evaluator.status["last_error"] or ""
        assert "sentinel-secret-do-not-leak-xyz" not in last_error
        for record in caplog.records:
            assert "sentinel-secret-do-not-leak-xyz" not in record.getMessage()

    def test_url_never_in_logs_on_delivery_failure(self, db_path, caplog):
        transport = _QueueTransport(responses=["raise"])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )
        _poll(evaluator, "acme", "acme.x", None, 100)

        with caplog.at_level(logging.DEBUG):
            _poll(evaluator, "acme", "acme.x", 100, 550)

        for record in caplog.records:
            assert SENTINEL_URL not in record.getMessage()

    def test_url_never_in_stored_pending_or_marker(self, db_path):
        transport = _QueueTransport(responses=[500])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )
        _poll(evaluator, "acme", "acme.x", None, 100)
        _poll(evaluator, "acme", "acme.x", 100, 550)

        for value in storage.get_state_prefix(db_path, "milestone").values():
            assert SENTINEL_URL not in value

    def test_url_never_in_stored_last_error(self, db_path):
        transport = _QueueTransport(responses=["raise"])
        _seed_series(db_path, "acme.x", value=100)
        evaluator, _ = _evaluator(
            db_path, [_rule("acme.x", every=500)], transport=transport
        )
        _poll(evaluator, "acme", "acme.x", None, 100)
        _poll(evaluator, "acme", "acme.x", 100, 550)

        assert evaluator.status["last_error"] is not None
        assert SENTINEL_URL not in evaluator.status["last_error"]
