import json
import logging
import threading
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

    def simulate_connect(self, reason_code=0):
        self.on_connect(self, None, {}, reason_code)

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
            "include": [],
            "exclude": [],
        }

    def test_non_mapping_raises_config_error(self):
        with pytest.raises(ConfigError):
            mqtt.validate_mqtt_config("broker.local")

    def test_include_and_exclude_are_carried_through(self):
        result = mqtt.validate_mqtt_config(
            {
                "host": "broker.local",
                "include": ["makerworld.*"],
                "exclude": ["makerworld.*.comments"],
            }
        )
        assert result["include"] == ["makerworld.*"]
        assert result["exclude"] == ["makerworld.*.comments"]

    def test_non_list_include_raises_config_error(self):
        with pytest.raises(ConfigError, match="include"):
            mqtt.validate_mqtt_config({"host": "broker.local", "include": "acme.*"})

    def test_non_string_exclude_entry_raises_config_error(self):
        with pytest.raises(ConfigError, match="exclude"):
            mqtt.validate_mqtt_config({"host": "broker.local", "exclude": [123]})

    def test_empty_string_pattern_raises_config_error(self):
        with pytest.raises(ConfigError, match="include"):
            mqtt.validate_mqtt_config({"host": "broker.local", "include": [""]})


class TestClientIdentity:
    def test_two_publishers_from_the_same_config_get_different_client_ids(
        self, db_path
    ):
        mqtt_config = {
            "host": "broker.local",
            "port": 1883,
            "topic_prefix": "numbers-go-up",
        }
        first = mqtt.MqttPublisher(mqtt_config, db_path, {}, 1800)
        second = mqtt.MqttPublisher(mqtt_config, db_path, {}, 1800)

        assert first._client._client_id != second._client._client_id
        # unique_id/object_id derivation is untouched by the client id --
        # unrelated to a broker session and must not orphan existing HA
        # entities.
        assert first._client._client_id.startswith(b"numbers-go-up-numbers-go-up-")


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
        # object_id is deprecated in HA Core 2026.4+ in favour of
        # default_entity_id (which must carry the domain prefix); publish
        # both so this works whether HA is old or new.
        assert (
            payload["default_entity_id"]
            == "sensor.ngu_makerworld_profile_design_downloads"
        )

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
            db_path, server_config={"external_url": "http://192.168.1.50:8080"}
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
            "configuration_url": "http://192.168.1.50:8080",
        }

    def test_device_block_omits_configuration_url_when_external_url_unset(
        self, db_path
    ):
        _seed_series(db_path, "acme.designs", value=1)
        publisher, fake = _publisher(db_path)

        publisher._republish_snapshot()

        payload = json.loads(
            fake.published_dict(
                "homeassistant/sensor/numbers_go_up/ngu_acme_designs/config"
            )
        )
        assert "configuration_url" not in payload["device"]

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


# --- include/exclude filtering ---------------------------------------------


class TestIncludeExcludeFiltering:
    def test_snapshot_publishes_only_included_series(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        _seed_series(db_path, "other.b", value=2)
        publisher, fake = _publisher(
            db_path, mqtt_config_overrides={"include": ["acme.*"]}
        )

        publisher._republish_snapshot()

        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"
        assert fake.published_dict("numbers-go-up/other.b/state") is None
        assert (
            fake.published_dict("homeassistant/sensor/numbers_go_up/ngu_other_b/config")
            is None
        )

    def test_exclude_wins_over_include(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        _seed_series(db_path, "acme.b", value=2)
        publisher, fake = _publisher(
            db_path,
            mqtt_config_overrides={"include": ["acme.*"], "exclude": ["acme.b"]},
        )

        publisher._republish_snapshot()

        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"
        assert fake.published_dict("numbers-go-up/acme.b/state") is None

    def test_exclude_alone_drops_matching_series(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        _seed_series(db_path, "acme.b", value=2)
        publisher, fake = _publisher(
            db_path, mqtt_config_overrides={"exclude": ["acme.b"]}
        )

        publisher._republish_snapshot()

        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"
        assert fake.published_dict("numbers-go-up/acme.b/state") is None

    def test_on_poll_finished_skips_excluded_key(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        _seed_series(db_path, "acme.b", value=2)
        publisher, fake = _publisher(
            db_path, mqtt_config_overrides={"exclude": ["acme.b"]}
        )

        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a", "acme.b"}))

        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"
        assert fake.published_dict("numbers-go-up/acme.b/state") is None

    def test_excluded_series_does_not_claim_its_object_id(self, db_path):
        # An excluded series is never published at all, so it must not
        # occupy the object_id slot a colliding, *included* series needs.
        _seed_series(db_path, "acme.a_b", value=1)
        publisher, fake = _publisher(
            db_path, mqtt_config_overrides={"exclude": ["acme.a_b"]}
        )
        publisher._republish_snapshot()
        fake.published.clear()

        _seed_series(db_path, "acme.a.b", value=2)
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a.b"}))

        assert fake.published_dict("numbers-go-up/acme.a.b/state") == "2"

    def test_excluded_inactive_series_does_not_claim_its_object_id_on_snapshot(
        self, db_path
    ):
        # Same hazard as test_excluded_series_does_not_claim_its_object_id,
        # but through the snapshot's removal path for an *inactive* series:
        # _republish_snapshot must not let an excluded-and-deactivated
        # series claim an object_id it will never actually publish to,
        # which would otherwise permanently steal the slot from a
        # legitimate, included, active colliding series.
        excluded_id = _seed_series(db_path, "acme.a_b", value=1)
        storage.set_series_active_bulk(db_path, [], [excluded_id])
        _seed_series(db_path, "acme.a.b", value=2)
        publisher, fake = _publisher(
            db_path, mqtt_config_overrides={"exclude": ["acme.a_b"]}
        )

        publisher._republish_snapshot()

        discovery_topic = "homeassistant/sensor/numbers_go_up/ngu_acme_a_b/config"
        payload = json.loads(fake.published_dict(discovery_topic))
        assert payload["unique_id"] == "ngu_acme_a_b"
        assert fake.published_dict("numbers-go-up/acme.a.b/state") == "2"

    def test_no_include_or_exclude_publishes_everything(self, db_path):
        _seed_series(db_path, "acme.a", value=1)
        _seed_series(db_path, "other.b", value=2)
        publisher, fake = _publisher(db_path)

        publisher._republish_snapshot()

        assert fake.published_dict("numbers-go-up/acme.a/state") == "1"
        assert fake.published_dict("numbers-go-up/other.b/state") == "2"


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

    def test_removal_is_not_gated_on_this_polls_status(self, db_path):
        # Deactivation is a DB fact set by a *previous* successful run's
        # pattern-series reconciliation. A later poll that happens to fail
        # must still publish the removal for a series it already knows was
        # deactivated -- otherwise the entity looks frozen-but-available in
        # HA (the process keeps publishing "online" on the availability
        # topic) until some future successful poll notices.
        series_id = _seed_series(db_path, "acme.a", value=1)
        publisher, fake = _publisher(db_path)
        publisher._connected = True
        publisher.on_poll_finished("acme", "ok", frozenset({"acme.a"}))

        storage.set_series_active_bulk(db_path, [], [series_id])
        fake.published.clear()
        publisher.on_poll_finished("acme", "error", frozenset())

        discovery_topic = "homeassistant/sensor/numbers_go_up/ngu_acme_a/config"
        assert fake.published_dict(discovery_topic) == b""
        assert fake.published_dict("numbers-go-up/acme.a/state") == b""
        assert fake.published_dict("numbers-go-up/acme.a/attrs") == b""

    def test_snapshot_reconciles_a_series_deactivated_while_the_process_was_down(
        self, db_path
    ):
        # _known_active starts empty on every process restart. A series
        # deactivated while the process was down (or before this process
        # ever touched it) must still be removed on the next full snapshot
        # -- reconciled against the DB, not against in-memory state.
        series_id = _seed_series(db_path, "acme.a", value=1)
        storage.set_series_active_bulk(db_path, [], [series_id])

        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()
        fake.simulate_connect()

        discovery_topic = "homeassistant/sensor/numbers_go_up/ngu_acme_a/config"
        assert fake.published_dict(discovery_topic) == b""
        assert fake.published_dict("numbers-go-up/acme.a/state") == b""
        assert fake.published_dict("numbers-go-up/acme.a/attrs") == b""


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

    def test_refused_connack_is_not_reported_as_connected(self, db_path):
        # paho dispatches on_connect for a *refused* CONNACK too -- rc=5
        # (not authorised) is the shape a wrong username/password takes.
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()

        fake.simulate_connect(reason_code=5)

        assert publisher.status["connected"] is False
        assert "connect refused" in publisher.status["last_error"]
        # No online/snapshot traffic for a connection that was refused.
        assert fake.published_dict("numbers-go-up/status") != b"online"

    def test_refused_connack_reason_code_object_with_is_failure(self, db_path):
        # Real paho hands the callback a ReasonCode with .is_failure, not a
        # plain int -- exercise that shape too, not just the fake's int rc.
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()

        fake.simulate_connect(reason_code=SimpleNamespace(is_failure=True))

        assert publisher.status["connected"] is False

    def test_successful_connack_reason_code_object_without_failure(self, db_path):
        publisher, fake = _publisher(db_path, connected=False)
        publisher.start()

        fake.simulate_connect(reason_code=SimpleNamespace(is_failure=False))

        assert publisher.status["connected"] is True


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


class TestConcurrentBookkeeping:
    def test_concurrent_poll_finished_and_snapshot_do_not_corrupt_state(self, db_path):
        # Same shape as production: scheduler threads call on_poll_finished
        # while the paho network thread calls _republish_snapshot (from
        # _on_connect), both mutating _object_id_owner/
        # _discovery_fingerprints/_known_active. Before the RLock, this
        # was a bare check-then-set race (#127); under the GIL it never
        # crashed, but nothing guaranteed it wouldn't. This hammers both
        # paths from real threads and asserts the bookkeeping ends up
        # internally consistent rather than merely "didn't raise".
        for i in range(20):
            _seed_series(db_path, f"acme.item{i}", value=i)
        publisher, _fake = _publisher(db_path, plugin_intervals={"acme": 1800})

        errors: list[Exception] = []

        def hammer_poll_finished():
            try:
                for _ in range(25):
                    publisher.on_poll_finished(
                        "acme", "ok", frozenset(f"acme.item{i}" for i in range(20))
                    )
            except Exception as exc:  # pragma: no cover - assertion below fails first
                errors.append(exc)

        def hammer_snapshot():
            try:
                for _ in range(25):
                    publisher._republish_snapshot()
            except Exception as exc:  # pragma: no cover - assertion below fails first
                errors.append(exc)

        threads = [
            threading.Thread(target=hammer_poll_finished),
            threading.Thread(target=hammer_snapshot),
            threading.Thread(target=hammer_poll_finished),
            threading.Thread(target=hammer_snapshot),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), "a thread deadlocked on the RLock"

        assert errors == []
        # Every known-active key has exactly one object_id owner, and every
        # owner points back at a key that's actually known-active or was
        # at some point -- no torn/partial updates from an unlocked race.
        assert set(publisher._object_id_owner.values()) >= publisher._known_active
