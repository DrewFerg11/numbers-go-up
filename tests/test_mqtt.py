import json
import logging
from types import SimpleNamespace

import pytest

from numbers_go_up import migrate, mqtt, storage
from numbers_go_up.config import ConfigError


class FakeMqttClient:
    """A ``paho.mqtt.client.Client``-shaped test double. No network access
    anywhere -- every method just records what was called."""

    def __init__(self, *args, **kwargs):
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.connected_to = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.published: list[tuple[str, object, bool]] = []
        self.subscribed: list[str] = []
        self.will: tuple[str, object, bool] | None = None
        self.username: str | None = None
        self.password: str | None = None
        self.tls_set_called = False
        self.connect_async_raises: Exception | None = None
        self.publish_raises: Exception | None = None

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def tls_set(self, *args, **kwargs):
        self.tls_set_called = True

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def connect_async(self, host, port, *args, **kwargs):
        if self.connect_async_raises:
            raise self.connect_async_raises
        self.connected_to = (host, port)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, *args, **kwargs):
        self.subscribed.append(topic)

    def publish(self, topic, payload=None, qos=0, retain=False):
        if self.publish_raises:
            raise self.publish_raises
        self.published.append((topic, payload, retain))
        return SimpleNamespace(rc=0)

    # -- test helpers, not part of the paho.Client surface --

    def simulate_connect(self):
        self.on_connect(self, None, {}, 0)

    def simulate_disconnect(self):
        self.on_disconnect(self, None, 0)

    def simulate_message(self, topic, payload):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))

    def published_dict(self, topic):
        for t, payload, _retain in reversed(self.published):
            if t == topic:
                return payload
        return None


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(path)
    return path


def _seed_series(
    db_path,
    metric_key,
    plugin_name="acme",
    kind="cumulative",
    label="Label",
    unit="widgets",
    icon="mdi:download",
    value=None,
    now=1000,
    attrs=None,
):
    storage.get_or_create_series(
        db_path, metric_key, plugin_name, kind, label, unit, icon, now, attrs=attrs
    )
    row = storage.get_series_by_key(db_path, metric_key)
    if value is not None:
        storage.record_sample(db_path, row["id"], now, value, 86400)
    return row["id"]


def _publisher(
    db_path, plugin_intervals=None, default_interval=1800, connected=True, **kwargs
):
    fake = FakeMqttClient()
    mqtt_config = {
        "host": "broker.local",
        "port": 1883,
        "username": None,
        "tls": False,
        "discovery_prefix": "homeassistant",
        "topic_prefix": "numbers-go-up",
    }
    mqtt_config.update(kwargs.pop("mqtt_config_overrides", {}))
    publisher = mqtt.MqttPublisher(
        mqtt_config,
        db_path,
        plugin_intervals or {"acme": 1800},
        default_interval,
        client=fake,
        **kwargs,
    )
    # Most tests exercise publish behaviour directly (calling
    # _republish_snapshot()/on_poll_finished() without going through the
    # real connect callback), so default to "already connected" -- the
    # handful of tests about disconnected behaviour opt out explicitly.
    publisher._connected = connected
    return publisher, fake


# --- MQTT off -----------------------------------------------------------


class TestMqttOff:
    def test_no_mqtt_block_returns_noop_publisher_and_builds_no_client(
        self, tmp_path, monkeypatch
    ):
        def _boom(*a, **k):
            raise AssertionError("paho.mqtt.client.Client must not be constructed")

        monkeypatch.setattr(mqtt.paho_mqtt, "Client", _boom)

        publisher = mqtt.build_publisher(
            {"storage": {"path": str(tmp_path / "x.db")}}, {}
        )

        assert isinstance(publisher, mqtt.NoopPublisher)
        assert publisher.status == {
            "enabled": False,
            "connected": False,
            "broker": None,
            "last_publish": None,
            "last_error": None,
        }

    def test_noop_publisher_hook_and_lifecycle_are_inert(self):
        publisher = mqtt.NoopPublisher()
        publisher.start()
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.x"}))
        publisher.stop()
        assert publisher.status["enabled"] is False


# --- config validation ----------------------------------------------------


class TestValidateMqttConfig:
    def test_missing_block_is_off(self):
        assert mqtt.validate_mqtt_config(None) is None

    def test_missing_host_raises_config_error(self):
        with pytest.raises(ConfigError, match="host"):
            mqtt.validate_mqtt_config({"port": 1883})

    def test_bad_port_raises_config_error(self):
        with pytest.raises(ConfigError, match="port"):
            mqtt.validate_mqtt_config({"host": "broker.local", "port": "1883"})

    def test_port_out_of_range_raises_config_error(self):
        with pytest.raises(ConfigError, match="port"):
            mqtt.validate_mqtt_config({"host": "broker.local", "port": 70000})

    def test_valid_config_fills_in_defaults(self):
        result = mqtt.validate_mqtt_config({"host": "broker.local"})
        assert result == {
            "host": "broker.local",
            "port": 1883,
            "username": None,
            "tls": False,
            "discovery_prefix": "homeassistant",
            "topic_prefix": "numbers-go-up",
        }

    def test_non_mapping_raises_config_error(self):
        with pytest.raises(ConfigError):
            mqtt.validate_mqtt_config("broker.local")


# --- discovery payload ----------------------------------------------------


class TestDiscoveryPayload:
    def test_cumulative_kind_maps_to_total_increasing(self, db_path):
        _seed_series(db_path, "acme.designs", kind="cumulative", value=5)
        publisher, fake = _publisher(db_path, plugin_intervals={"acme": 1800})

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_designs/config"
            )
        )
        assert payload["state_class"] == "total_increasing"

    def test_gauge_kind_maps_to_measurement(self, db_path):
        _seed_series(db_path, "acme.temp", kind="gauge", value=5)
        publisher, fake = _publisher(db_path)

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_temp/config"
            )
        )
        assert payload["state_class"] == "measurement"

    def test_unique_id_and_object_id_are_dot_to_underscore_prefixed(self, db_path):
        _seed_series(db_path, "makerworld.profile.design_downloads", value=1)
        publisher, fake = _publisher(db_path, plugin_intervals={"makerworld": 1800})

        publisher._republish_snapshot()

        topic = (
            "homeassistant/sensor/numbers_go_up/"
            "ngu_makerworld_profile_design_downloads/config"
        )
        payload = json.loads(fake.published_dict(topic))
        assert payload["unique_id"] == "ngu_makerworld_profile_design_downloads"
        assert payload["object_id"] == "ngu_makerworld_profile_design_downloads"

    def test_expire_after_is_three_times_the_plugin_interval(self, db_path):
        _seed_series(db_path, "acme.designs", value=1)
        publisher, fake = _publisher(db_path, plugin_intervals={"acme": 900})

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_designs/config"
            )
        )
        assert payload["expire_after"] == 2700

    def test_device_block_matches_spec(self, db_path):
        _seed_series(db_path, "acme.designs", value=1)
        publisher, fake = _publisher(
            db_path, server_config={"host": "0.0.0.0", "port": 8080}
        )

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_designs/config"
            )
        )
        assert payload["device"] == {
            "identifiers": ["numbers-go-up"],
            "name": "numbers-go-up",
            "manufacturer": "numbers-go-up",
            "model": "stats poller",
            "sw_version": mqtt.__version__,
            "configuration_url": "http://localhost:8080/",
        }

    def test_availability_topic_is_the_status_topic(self, db_path):
        _seed_series(db_path, "acme.designs", value=1)
        publisher, fake = _publisher(db_path)

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_designs/config"
            )
        )
        assert payload["availability_topic"] == "numbers-go-up/status"
        assert payload["state_topic"] == "numbers-go-up/acme.designs/state"
        assert payload["json_attributes_topic"] == "numbers-go-up/acme.designs/attrs"

    def test_mdi_icon_is_included(self, db_path):
        _seed_series(db_path, "acme.designs", icon="mdi:download", value=1)
        publisher, fake = _publisher(db_path)

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_designs/config"
            )
        )
        assert payload["icon"] == "mdi:download"

    def test_non_mdi_icon_is_omitted(self, db_path):
        _seed_series(db_path, "acme.designs", icon="not-an-mdi-icon", value=1)
        publisher, fake = _publisher(db_path)

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_designs/config"
            )
        )
        assert "icon" not in payload

    def test_no_icon_is_omitted(self, db_path):
        _seed_series(db_path, "acme.designs", icon=None, value=1)
        publisher, fake = _publisher(db_path)

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_designs/config"
            )
        )
        assert "icon" not in payload


# --- object_id collisions --------------------------------------------------


class TestObjectIdCollisions:
    def test_second_colliding_series_is_skipped_with_one_error_log(
        self, db_path, caplog
    ):
        _seed_series(db_path, "a.b_c", value=1)
        _seed_series(db_path, "a_b.c", value=2)
        publisher, fake = _publisher(db_path)

        with caplog.at_level(logging.ERROR, logger="numbers_go_up.mqtt"):
            publisher._republish_snapshot()

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "a.b_c" in errors[0].getMessage()
        assert "a_b.c" in errors[0].getMessage()

        # first (alphabetically first) key wins and is untouched
        topic = "homeassistant/sensor/numbers_go_up/ngu_a_b_c/config"
        payload = json.loads(fake.published_dict(topic))
        assert payload["unique_id"] == "ngu_a_b_c"
        # the loser never got a state publish either
        assert fake.published_dict("numbers-go-up/a_b.c/state") is None
        assert fake.published_dict("numbers-go-up/a.b_c/state") is not None

    def test_collision_is_logged_only_once(self, db_path, caplog):
        _seed_series(db_path, "a.b_c", value=1)
        _seed_series(db_path, "a_b.c", value=2)
        publisher, fake = _publisher(db_path)

        with caplog.at_level(logging.ERROR, logger="numbers_go_up.mqtt"):
            publisher._republish_snapshot()
            publisher._republish_snapshot()

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1


# --- poll hook: state publishing ------------------------------------------


class TestOnPollFinished:
    def test_successful_poll_publishes_state_for_every_returned_key(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        _seed_series(db_path, "acme.b", value=2)
        publisher, fake = _publisher(db_path)
        publisher._connected = True

        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a", "acme.b"}))

        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"
        assert fake.published_dict("numbers-go-up/acme.b/state") == "2"

    def test_unchanged_value_is_still_published(self, db_path):
        # storage.py's store-on-change is orthogonal to this publisher:
        # every returned key gets a state publish, changed or not.
        _seed_series(db_path, "acme.a", value=1)
        publisher, fake = _publisher(db_path)
        publisher._connected = True

        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))
        fake.published.clear()
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))

        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"

    def test_failed_poll_publishes_nothing(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        publisher, fake = _publisher(db_path)
        publisher._connected = True

        publisher.on_poll_finished("acme", "error", frozenset({"acme.a"}))

        assert fake.published == []

    def test_discovery_republished_only_when_label_unit_or_icon_changes(self, db_path):
        _seed_series(db_path, "acme.a", value=1, label="Original")
        publisher, fake = _publisher(db_path)
        publisher._connected = True

        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))
        discovery_topic = "homeassistant/sensor/numbers_go_up/ngu_acme_a/config"
        first_count = sum(1 for t, _, _ in fake.published if t == discovery_topic)
        assert first_count == 1

        # same label/unit/icon: no re-publish of discovery
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))
        second_count = sum(1 for t, _, _ in fake.published if t == discovery_topic)
        assert second_count == 1

        # label changes: discovery republished
        _seed_series(db_path, "acme.a", value=1, label="Changed")
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))
        third_count = sum(1 for t, _, _ in fake.published if t == discovery_topic)
        assert third_count == 2

    def test_attrs_are_published_alongside_state(self, db_path):
        _seed_series(db_path, "acme.a", value=1, attrs={"foo": "bar"})
        publisher, fake = _publisher(db_path)
        publisher._connected = True

        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))

        attrs = json.loads(fake.published_dict("numbers-go-up/acme.a/attrs"))
        assert attrs == {"foo": "bar"}


# --- deactivation / reactivation -------------------------------------------


class TestDeactivationLifecycle:
    def test_deactivation_publishes_empty_retained_config_and_clears_state(
        self, db_path
    ):
        series_id = _seed_series(db_path, "acme.a", value=1)
        publisher, fake = _publisher(db_path)
        publisher._connected = True
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))
        fake.published.clear()

        storage.set_series_active_bulk(db_path, [], [series_id])
        publisher.on_poll_finished("acme", "ok", frozenset())

        discovery_topic = "homeassistant/sensor/numbers_go_up/ngu_acme_a/config"
        assert fake.published_dict(discovery_topic) == b""
        assert fake.published_dict("numbers-go-up/acme.a/state") == b""
        assert fake.published_dict("numbers-go-up/acme.a/attrs") == b""

    def test_reactivation_republishes_with_the_same_unique_id(self, db_path):
        series_id = _seed_series(db_path, "acme.a", value=1)
        publisher, fake = _publisher(db_path)
        publisher._connected = True
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))

        storage.set_series_active_bulk(db_path, [], [series_id])
        publisher.on_poll_finished("acme", "ok", frozenset())

        storage.set_series_active_bulk(db_path, [series_id], [])
        storage.record_sample(db_path, series_id, 2000, 42, 86400)
        fake.published.clear()
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))

        discovery_topic = "homeassistant/sensor/numbers_go_up/ngu_acme_a/config"
        payload = json.loads(fake.published_dict(discovery_topic))
        assert payload["unique_id"] == "ngu_acme_a"
        assert fake.published_dict("numbers-go-up/acme.a/state") == "42"


# --- connect / reconnect / HA restart --------------------------------------


class TestConnectionLifecycle:
    def test_connect_publishes_full_snapshot(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        _seed_series(db_path, "acme.b", value=2)
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()

        fake.simulate_connect()

        assert fake.published_dict("numbers-go-up/status") == b"online"
        assert (
            fake.published_dict("homeassistant/sensor/numbers_go_up/ngu_acme_a/config")
            is not None
        )
        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"
        assert fake.published_dict("numbers-go-up/acme.b/state") == "2"
        assert publisher.status["connected"] is True

    def test_connect_subscribes_to_discovery_status(self, db_path):
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()

        fake.simulate_connect()

        assert "homeassistant/status" in fake.subscribed

    def test_homeassistant_online_triggers_full_republish(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()
        fake.simulate_connect()
        fake.published.clear()

        fake.simulate_message("homeassistant/status", b"online")

        assert (
            fake.published_dict("homeassistant/sensor/numbers_go_up/ngu_acme_a/config")
            is not None
        )
        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"

    def test_lwt_is_offline_retained_on_status_topic(self, db_path):
        publisher, fake = _publisher(db_path)

        assert fake.will == ("numbers-go-up/status", b"offline", True)

    def test_disconnect_and_reconnect_updates_status(self, db_path):
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()
        fake.simulate_connect()
        assert publisher.status["connected"] is True

        fake.simulate_disconnect()
        assert publisher.status["connected"] is False

    def test_disconnected_publisher_does_not_call_client_publish(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        publisher, fake = _publisher(db_path, connected=False)
        # Never connected: publish() must no-op rather than queue.

        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))

        assert fake.published == []

    def test_stop_publishes_offline_status(self, db_path):
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()
        fake.simulate_connect()
        fake.published.clear()

        publisher.stop()

        assert fake.published_dict("numbers-go-up/status") == b"offline"
        assert fake.loop_stopped is True
        assert fake.disconnected is True


# --- unreachable broker at startup -----------------------------------------


class TestUnreachableBroker:
    def test_status_reports_disconnected_with_error_when_broker_unreachable(
        self, db_path
    ):
        publisher, fake = _publisher(db_path, connected=False)
        fake.connect_async_raises = OSError("Connection refused")

        publisher.start()

        status = publisher.status
        assert status["connected"] is False
        assert status["last_error"] is not None

    def test_recovery_publishes_full_snapshot_once_broker_comes_back(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()

        # simulate the broker "coming back": on_connect fires from paho's
        # own network thread once it succeeds.
        fake.simulate_connect()

        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"
        assert publisher.status["connected"] is True


# --- password never leaks --------------------------------------------------


class TestPasswordNeverLeaks:
    SENTINEL = "sentinel-password-xyz-do-not-leak-123"

    def test_password_reaches_only_username_pw_set(self, db_path):
        publisher, fake = _publisher(
            db_path,
            password=self.SENTINEL,
            mqtt_config_overrides={"username": "ngu"},
        )

        assert fake.password == self.SENTINEL

    def test_password_never_in_status(self, db_path):
        publisher, fake = _publisher(
            db_path,
            password=self.SENTINEL,
            mqtt_config_overrides={"username": "ngu"},
        )

        assert self.SENTINEL not in json.dumps(publisher.status)

    def test_password_never_in_logs(self, db_path, caplog):
        publisher, fake = _publisher(
            db_path,
            password=self.SENTINEL,
            mqtt_config_overrides={"username": "ngu"},
        )
        with caplog.at_level(logging.DEBUG):
            publisher.start()
            fake.simulate_connect()
            fake.simulate_disconnect()
            publisher.stop()

        for record in caplog.records:
            assert self.SENTINEL not in record.getMessage()

    def test_publish_failure_never_raises_and_status_stays_password_free(self, db_path):
        publisher, fake = _publisher(
            db_path,
            password=self.SENTINEL,
            mqtt_config_overrides={"username": "ngu"},
        )
        publisher._connected = True
        fake.publish_raises = RuntimeError("broker rejected publish")
        _seed_series(db_path, "acme.a", value=1)

        # Must not raise -- a publish failure never fails a plugin run --
        # and the resulting status must still never mention the password.
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))

        assert self.SENTINEL not in json.dumps(publisher.status)

    def test_build_publisher_reads_password_only_from_env(self, tmp_path):
        env = {mqtt.ENV_MQTT_PASSWORD: self.SENTINEL}
        config = {
            "mqtt": {"host": "broker.local", "username": "ngu"},
            "storage": {"path": str(tmp_path / "x.db")},
        }
        fake = FakeMqttClient()

        mqtt.build_publisher(config, {}, env=env, client=fake)

        assert fake.password == self.SENTINEL
