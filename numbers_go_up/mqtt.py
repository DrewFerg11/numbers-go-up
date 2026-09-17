"""MQTT discovery publisher for Home Assistant.

Turns every active series into a Home Assistant sensor automatically via
`MQTT discovery
<https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery>`_, grouped
under one "numbers-go-up" device, with the right ``state_class`` so the
values land in HA's long-term statistics. No YAML on the HA side.

This is the only module that reads the ``NGU_MQTT_PASSWORD`` environment
variable, and it never logs it or puts it in an exception message -- it is
handed straight to paho's ``username_pw_set`` and nowhere else.

The client runs on its own network thread (``loop_start()``); there's no
asyncio bridge, matching the thread-based scheduler (APScheduler
``BackgroundScheduler``) this project already uses elsewhere. A broker
that's unreachable must never fail startup or touch polling: every publish
is best-effort, skipped outright while disconnected (paho reconnects with
its own 1s-5min backoff), and the DB is the source of truth -- a full
snapshot is republished from it on every (re)connect.

When ``config["mqtt"]`` is absent, :func:`build_publisher` returns a
:class:`NoopPublisher` with the same interface, so callers (the scheduler,
``main.py``) never need an ``if mqtt:`` branch.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import paho.mqtt.client as paho_mqtt

from numbers_go_up import __version__, storage
from numbers_go_up.config import ConfigError

logger = logging.getLogger(__name__)

ENV_MQTT_PASSWORD = "NGU_MQTT_PASSWORD"

DEFAULT_PORT = 1883
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_TOPIC_PREFIX = "numbers-go-up"

# Grouping device for every entity this project publishes. Fixed, not
# derived from anything user-supplied.
DEVICE_IDENTIFIER = "numbers-go-up"
DEVICE_MODEL = "stats poller"

_KIND_TO_STATE_CLASS = {"cumulative": "total_increasing", "gauge": "measurement"}


class Publisher(Protocol):
    """The interface the scheduler calls after every finished poll."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def on_poll_finished(
        self, plugin_name: str, status: str, returned_keys: frozenset[str]
    ) -> None: ...

    @property
    def status(self) -> dict[str, Any]: ...


class NoopPublisher:
    """MQTT off. Every call is a no-op, so scheduler.py and main.py never
    need to branch on whether MQTT is configured."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def on_poll_finished(
        self, plugin_name: str, status: str, returned_keys: frozenset[str]
    ) -> None:
        pass

    @property
    def status(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "connected": False,
            "broker": None,
            "last_publish": None,
            "last_error": None,
        }


def validate_mqtt_config(raw: Any) -> dict[str, Any] | None:
    """Validate and normalise ``config["mqtt"]``.

    Returns ``None`` when the block is absent (MQTT off, zero connections
    -- the config contract). Raises :class:`ConfigError` for anything else
    that's wrong, so a bad broker *config* fails startup while a bad broker
    *connection* never does -- that's :class:`MqttPublisher`'s job, not
    this function's.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"mqtt config must be a mapping, got {type(raw).__name__}")

    host = raw.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("mqtt.host is required and must be a non-empty string")

    port = raw.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not (0 < port < 65536):
        raise ConfigError(f"mqtt.port must be an integer in 1-65535, got {port!r}")

    username = raw.get("username")
    if username is not None and not isinstance(username, str):
        raise ConfigError(
            f"mqtt.username must be a string, got {type(username).__name__}"
        )

    tls = raw.get("tls", False)
    if not isinstance(tls, bool):
        raise ConfigError(f"mqtt.tls must be a boolean, got {type(tls).__name__}")

    discovery_prefix = raw.get("discovery_prefix", DEFAULT_DISCOVERY_PREFIX)
    if not isinstance(discovery_prefix, str) or not discovery_prefix.strip():
        raise ConfigError("mqtt.discovery_prefix must be a non-empty string")

    topic_prefix = raw.get("topic_prefix", DEFAULT_TOPIC_PREFIX)
    if not isinstance(topic_prefix, str) or not topic_prefix.strip():
        raise ConfigError("mqtt.topic_prefix must be a non-empty string")

    return {
        "host": host,
        "port": port,
        "username": username,
        "tls": tls,
        "discovery_prefix": discovery_prefix,
        "topic_prefix": topic_prefix,
    }


def _format_number(value: float) -> str:
    """A plain number as a string. Values round-trip through SQLite as
    floats even when they started out as ints (``storage.record_sample``),
    so an integral float is rendered without the trailing ``.0`` -- ``42``,
    not ``42.0`` -- matching what the source plugin actually reported."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value)


class MqttPublisher:
    """Publishes MQTT discovery + state for every active series.

    ``client`` is injectable so tests can hand in a fake
    ``paho.mqtt.client.Client``-shaped double instead of touching a real
    broker; when omitted, a real ``paho.mqtt.client.Client`` is built.
    """

    def __init__(
        self,
        mqtt_config: dict[str, Any],
        db_path: str | Path,
        plugin_intervals: dict[str, int],
        default_interval: int,
        server_config: dict[str, Any] | None = None,
        password: str | None = None,
        version: str = __version__,
        client: Any = None,
    ) -> None:
        self._host = mqtt_config["host"]
        self._port = mqtt_config["port"]
        self._username = mqtt_config.get("username")
        self._tls = mqtt_config.get("tls", False)
        self._discovery_prefix = mqtt_config.get(
            "discovery_prefix", DEFAULT_DISCOVERY_PREFIX
        )
        self._topic_prefix = mqtt_config.get("topic_prefix", DEFAULT_TOPIC_PREFIX)
        self._db_path = db_path
        self._plugin_intervals = plugin_intervals
        self._default_interval = default_interval
        self._password = password
        self._version = version

        server_config = server_config or {}
        server_host = server_config.get("host") or "0.0.0.0"
        # 0.0.0.0/:: is a bind address, not something a browser can open --
        # fall back to localhost for the device's configuration_url link.
        if server_host in ("0.0.0.0", "::"):
            server_host = "localhost"
        server_port = server_config.get("port", 8080)
        self._configuration_url = f"http://{server_host}:{server_port}/"

        self._lock = threading.Lock()
        self._connected = False
        self._last_publish: float | None = None
        self._last_error: str | None = None

        # metric_key -> object_id, once assigned, forever -- see
        # _object_id_for. Never cleared, even across deactivation, so a
        # collision loser stays skipped and a permanent record of "who owns
        # this object_id" survives reconnects.
        self._object_id_owner: dict[str, str] = {}
        self._collision_logged: set[str] = set()
        # metric_key -> (label, unit, icon, kind, expire_after), the last
        # discovery payload's identifying fields -- lets on_poll_finished
        # skip re-publishing discovery for a series that hasn't changed.
        self._discovery_fingerprints: dict[str, tuple[Any, ...]] = {}
        # metric_keys we currently believe have a live discovery config
        # published (i.e. not removed), so on_poll_finished knows which
        # newly-deactivated series need a removal message.
        self._known_active: set[str] = set()

        self._client = client if client is not None else self._build_client()
        self._configure_client()

    # -- construction ------------------------------------------------

    def _build_client(self) -> Any:
        return paho_mqtt.Client(
            paho_mqtt.CallbackAPIVersion.VERSION2, client_id="numbers-go-up"
        )

    def _configure_client(self) -> None:
        client = self._client
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        if self._username:
            client.username_pw_set(self._username, self._password)
        if self._tls:
            # System CAs, no client cert -- the documented "port 8883" case.
            client.tls_set()
        client.will_set(self._status_topic(), payload=b"offline", retain=True)

    # -- topics --------------------------------------------------------

    def _status_topic(self) -> str:
        return f"{self._topic_prefix}/status"

    def _state_topic(self, metric_key: str) -> str:
        return f"{self._topic_prefix}/{metric_key}/state"

    def _attrs_topic(self, metric_key: str) -> str:
        return f"{self._topic_prefix}/{metric_key}/attrs"

    def _discovery_topic(self, object_id: str) -> str:
        return f"{self._discovery_prefix}/sensor/numbers_go_up/{object_id}/config"

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        try:
            self._client.connect_async(self._host, self._port)
            self._client.loop_start()
        except Exception as exc:  # never fail startup on a bad broker
            with self._lock:
                self._last_error = f"connect failed: {exc}"
            logger.warning("MQTT: failed to start client: %s", exc)

    def stop(self) -> None:
        self._publish(self._status_topic(), b"offline", retain=True)
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            logger.debug("MQTT: error during disconnect", exc_info=True)
        with self._lock:
            self._connected = False

    # -- paho callbacks ----------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, *args) -> None:
        with self._lock:
            was_connected = self._connected
            self._connected = True
            self._last_error = None
        if not was_connected:
            logger.info("MQTT connected to %s:%s", self._host, self._port)
        try:
            client.subscribe(f"{self._discovery_prefix}/status")
        except Exception:
            logger.debug("MQTT: subscribe to discovery status failed", exc_info=True)
        self._publish(self._status_topic(), b"online", retain=True)
        self._republish_snapshot()

    def _on_disconnect(self, client, userdata, *args) -> None:
        with self._lock:
            was_connected = self._connected
            self._connected = False
        if was_connected:
            logger.warning("MQTT disconnected from %s:%s", self._host, self._port)

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic == f"{self._discovery_prefix}/status" and msg.payload == b"online":
            logger.info(
                "Home Assistant MQTT integration came back online; "
                "republishing full snapshot"
            )
            self._republish_snapshot()

    # -- publishing ----------------------------------------------------

    def _publish(self, topic: str, payload: Any, retain: bool) -> None:
        """Best-effort publish: never raises, never queues while
        disconnected (paho's own reconnect handles that; a full snapshot is
        republished from the DB -- the source of truth -- on reconnect)."""
        with self._lock:
            connected = self._connected
        if not connected:
            return
        try:
            self._client.publish(topic, payload, qos=1, retain=retain)
        except Exception as exc:
            with self._lock:
                self._last_error = f"publish to {topic} failed: {exc}"
            logger.debug("MQTT publish to %s failed: %s", topic, exc)
            return
        with self._lock:
            self._last_publish = time.time()

    def _object_id_for(self, metric_key: str) -> str | None:
        """The object_id/unique_id for ``metric_key``, or ``None`` if it
        collides with an already-assigned one (``a.b_c`` vs ``a_b.c`` both
        becoming ``ngu_a_b_c``).

        Assignment is first-come, first-served and permanent -- the first
        metric_key to claim an object_id keeps it for the life of the
        process; the loser is logged once, with an ERROR line naming both
        keys, and skipped forever (never overwrites the winner).
        """
        object_id = "ngu_" + metric_key.replace(".", "_")
        owner = self._object_id_owner.get(object_id)
        if owner is None:
            self._object_id_owner[object_id] = metric_key
            return object_id
        if owner == metric_key:
            return object_id

        if metric_key not in self._collision_logged:
            logger.error(
                "MQTT discovery: metric %r and %r both map to object_id %r; "
                "keeping %r, skipping %r",
                owner,
                metric_key,
                object_id,
                owner,
                metric_key,
            )
            self._collision_logged.add(metric_key)
        return None

    def _expire_after(self, plugin_name: str) -> int:
        # Same rule as api.py's _is_stale: 3x the plugin's poll interval.
        interval = self._plugin_intervals.get(plugin_name, self._default_interval)
        return 3 * interval

    def _discovery_payload(self, row: Any, object_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": row["label"],
            "unique_id": object_id,
            # HA has been moving MQTT discovery from `object_id` to
            # `default_entity_id` in recent releases. We use `object_id`
            # here: it's still broadly supported across current HA
            # versions and this environment has no network access to
            # confirm the very latest docs against -- a maintainer on a
            # newer HA build should double check whether `default_entity_id`
            # is now preferred and adjust if so (see the PR description).
            "object_id": object_id,
            "state_topic": self._state_topic(row["metric_key"]),
            "json_attributes_topic": self._attrs_topic(row["metric_key"]),
            "availability_topic": self._status_topic(),
            "state_class": _KIND_TO_STATE_CLASS[row["kind"]],
            "suggested_display_precision": 0,
            "expire_after": self._expire_after(row["plugin_name"]),
            "device": {
                "identifiers": [DEVICE_IDENTIFIER],
                "name": DEVICE_IDENTIFIER,
                "manufacturer": DEVICE_IDENTIFIER,
                "model": DEVICE_MODEL,
                "sw_version": self._version,
                "configuration_url": self._configuration_url,
            },
        }
        if row["unit"]:
            payload["unit_of_measurement"] = row["unit"]
        icon = row["icon"]
        if isinstance(icon, str) and icon.startswith("mdi:"):
            payload["icon"] = icon
        return payload

    def _discovery_fingerprint(self, row: Any) -> tuple[Any, ...]:
        return (
            row["label"],
            row["unit"],
            row["icon"],
            row["kind"],
            self._expire_after(row["plugin_name"]),
        )

    def _maybe_publish_discovery(self, row: Any, force: bool = False) -> None:
        key = row["metric_key"]
        object_id = self._object_id_for(key)
        if object_id is None:
            return

        fingerprint = self._discovery_fingerprint(row)
        if not force and self._discovery_fingerprints.get(key) == fingerprint:
            self._known_active.add(key)
            return

        payload = self._discovery_payload(row, object_id)
        self._publish(
            self._discovery_topic(object_id), json.dumps(payload), retain=True
        )
        self._discovery_fingerprints[key] = fingerprint
        self._known_active.add(key)

    def _publish_state(self, row: Any) -> None:
        if row["last_value"] is None:
            return
        key = row["metric_key"]
        self._publish(
            self._state_topic(key), _format_number(row["last_value"]), retain=True
        )
        attrs_json = row["attrs"] if row["attrs"] else "{}"
        self._publish(self._attrs_topic(key), attrs_json, retain=True)

    def _publish_removal(self, metric_key: str) -> None:
        object_id = self._object_id_for(metric_key)
        if object_id is not None:
            self._publish(self._discovery_topic(object_id), b"", retain=True)
        self._publish(self._state_topic(metric_key), b"", retain=True)
        self._publish(self._attrs_topic(metric_key), b"", retain=True)
        self._discovery_fingerprints.pop(metric_key, None)
        self._known_active.discard(metric_key)

    # -- snapshots ----------------------------------------------------

    def _republish_snapshot(self) -> None:
        """Discovery for every active series, then every state+attrs.
        Called on connect/reconnect and when Home Assistant's own MQTT
        integration announces ``online``. Always republishes discovery
        (``force=True``) -- the broker's retained state may have been lost,
        and this process may just be starting up."""
        try:
            rows = [
                row for row in storage.list_all_series(self._db_path) if row["active"]
            ]
        except Exception:
            logger.exception("MQTT: failed to load series for snapshot republish")
            return

        for row in rows:
            self._maybe_publish_discovery(row, force=True)
        for row in rows:
            # A collision loser (no object_id) has no entity to carry its
            # state -- nothing to publish it to.
            if self._object_id_for(row["metric_key"]) is not None:
                self._publish_state(row)

    # -- scheduler hook ----------------------------------------------------

    def on_poll_finished(
        self, plugin_name: str, status: str, returned_keys: frozenset[str]
    ) -> None:
        """Called by the scheduler after every finished poll.

        After a successful poll: state (and, when new/changed, discovery)
        for every series the poll returned -- including unchanged values,
        since store-on-change is a storage.py concern, not this publisher's.
        A deactivated series (the pattern-series lifecycle) gets its
        discovery config and state/attrs cleared. After a failed poll,
        nothing is published. Never raises -- a publish failure must never
        fail a plugin run.
        """
        if status != "ok":
            return
        try:
            self._handle_poll_finished(plugin_name, returned_keys)
        except Exception as exc:
            with self._lock:
                self._last_error = f"publish error: {exc}"
            logger.debug(
                "MQTT: error handling poll finish for %s: %s", plugin_name, exc
            )

    def _handle_poll_finished(
        self, plugin_name: str, returned_keys: frozenset[str]
    ) -> None:
        try:
            rows = {
                row["metric_key"]: row
                for row in storage.list_all_series(self._db_path)
                if row["plugin_name"] == plugin_name
            }
        except Exception:
            logger.exception(
                "MQTT: failed to load series for plugin %s after poll", plugin_name
            )
            return

        for key, row in rows.items():
            if row["active"]:
                if key in returned_keys:
                    self._maybe_publish_discovery(row)
                    if self._object_id_for(key) is not None:
                        self._publish_state(row)
            elif key in self._known_active:
                self._publish_removal(key)

    @property
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "connected": self._connected,
                "broker": f"{self._host}:{self._port}",
                "last_publish": self._last_publish,
                "last_error": self._last_error,
            }


def build_publisher(
    config: dict[str, Any],
    plugin_intervals: dict[str, int],
    env: Mapping[str, str] | None = None,
    client: Any = None,
) -> Publisher:
    """Build the publisher ``main.py`` and the scheduler use.

    Returns a :class:`NoopPublisher` when ``config["mqtt"]`` is absent --
    MQTT off, zero connections. Raises :class:`ConfigError` for a present
    but invalid ``mqtt`` block; never for an unreachable broker, which is
    only ever discovered later, on the network thread.
    """
    mqtt_config = validate_mqtt_config(config.get("mqtt"))
    if mqtt_config is None:
        return NoopPublisher()

    env = os.environ if env is None else env
    password = env.get(ENV_MQTT_PASSWORD)
    default_interval = (config.get("poll") or {}).get("default_interval", 1800)

    return MqttPublisher(
        mqtt_config,
        config["storage"]["path"],
        plugin_intervals,
        default_interval,
        server_config=config.get("server"),
        password=password,
        client=client,
    )
