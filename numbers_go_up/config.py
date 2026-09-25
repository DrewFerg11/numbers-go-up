"""Configuration loading: env vars, YAML file, and defaults.

This is the only module that resolves a filesystem path directly.
A handful of other modules read their own ``NGU_*`` env var directly
rather than through this module's dict, each documented at its own
read: ``main.NGU_LOG_LEVEL``, ``mqtt.NGU_MQTT_PASSWORD`` (a secret,
deliberately kept out of ``config.yaml``), and
``netfs.NGU_ALLOW_NETWORK_FS``. Everything else — storage, plugins,
the scheduler — gets its settings from the dict this module returns.

The core sections (``poll``, ``storage``, ``server``, ``dashboard``) are
validated as a Pydantic model tree, with ``extra="forbid"`` so an unknown
key -- a typo'd ``mqqt:`` or a quoted ``heartbeat_seconds: "86400"`` -- fails
startup instead of silently doing nothing or crashing on the second poll.
Set ``NGU_CONFIG_STRICT=0`` to downgrade a failed top-level/unknown-key
check to a warning and start anyway, as a temporary escape hatch for a
config that worked before this validation existed -- meant for getting an
existing install running again long enough to fix the config properly, not
as a standing setting: left on, it silently accepts the exact class of
typo (a misspelled section name, a quoted number) this validation exists
to catch, which is why it defaults to on (strict) and has to be opted out
of explicitly. ``mqtt``/``milestones``/``plugins`` stay loosely-typed
dicts here; their own modules validate their contents.
"""

from __future__ import annotations

import copy
import difflib
import logging
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

logger = logging.getLogger(__name__)

ENV_DATA_DIR = "NGU_DATA_DIR"
ENV_CONFIG_FILE = "NGU_CONFIG_FILE"
ENV_PLUGIN_DIR = "NGU_PLUGIN_DIR"
ENV_CONFIG_STRICT = "NGU_CONFIG_STRICT"

DEFAULT_DATA_DIR = "/data"
DEFAULT_CONFIG_FILE = "/config/config.yaml"
DEFAULT_PLUGIN_DIR = "/plugins"

EXAMPLE_CONFIG_FILENAME = "config.yaml.example"

# Kept identical to the repo-root config.yaml.example (enforced by
# tests/test_config.py) so the file the app writes on first run and the one
# a human reads in the repo never drift apart.
EXAMPLE_CONFIG = """\
# numbers-go-up configuration.
#
# This file is optional. If it's missing, the service starts with the
# built-in defaults below and zero plugins enabled -- no default user IDs,
# handles, or repos ever ship in the image.
#
# All intervals are in seconds.
#
# An unknown top-level key (a typo like "mqqt:") fails startup, so a mistake
# doesn't silently do nothing. Set NGU_CONFIG_STRICT=0 (an env var, not a
# config key) to downgrade that to a warning and start anyway -- a
# temporary escape hatch to get a broken config booting again while you
# fix it, not something to leave set: it accepts the exact typos this
# check exists to catch.

# poll:
#   default_interval: 1800      # 30 min
#   jitter_fraction: 0.2        # +/-20%
#
# storage:
#   heartbeat_seconds: 86400    # force one sample/day even if unchanged
#   plugin_runs_retention_days: 30
#   backups:
#     keep_daily: 7              # daily DB backups to keep; 0 disables them
#
# server:
#   external_url: "http://192.168.1.50:8080"   # optional; feeds Home Assistant's
#                                                # device "Visit" link. The container
#                                                # always listens on 0.0.0.0:8080 --
#                                                # change the outside port with
#                                                # compose's port mapping
#                                                # ("9000:8080"), not here.
#
# plugins:
#   makerworld:
#     enabled: true              # every plugin is OFF until explicitly enabled
#     user_id: "your-makerworld-user-id"
#     poll_interval: 1800
#     models:
#       enabled: false           # opt-in: adds one paged listing request/poll
#       include: []              # model IDs to track; empty = every published model
#       max: 50                  # cardinality guard, applied after filtering
#
#   github:
#     enabled: true              # every plugin is OFF until explicitly enabled
#     repos: ["owner/name"]      # "owner/name" strings; renames are followed
#     release_downloads: false   # true adds paged release-asset requests/poll
#     max: 20                    # cardinality guard on repos
#     # Optional token: set the NGU_GITHUB_TOKEN env var, never here, to
#     # raise the unauthenticated 60/hr budget to 5,000/hr.
#
#   youtube:
#     enabled: true              # every plugin is OFF until explicitly enabled
#     source: official           # the only source this plugin ships
#     channels:
#       - key: main              # metric-key slug: [a-z0-9_-]+, permanent
#         id: "UCxxxxxxxxxxxxxxxxxxxxxx"   # case-sensitive; not a handle/URL
#     max: 5                     # cardinality guard on channels
#     videos:                    # optional, independent of channels -- views/likes
#       - key: launch_video      # metric-key slug: [a-z0-9_-]+, permanent
#         id: "dQw4w9WgXcQ"      # the 11-character id from the video's URL
#     videos_max: 20             # cardinality guard on videos
#     # Required: set the NGU_YOUTUBE_API_KEY env var, never here.
#
#   abacus:
#     enabled: true              # every plugin is OFF until explicitly enabled
#     base_url: https://abacus.jasoncameron.dev   # self-hosted instance override
#     counters:
#       - key: sfd_flash_factory    # metric-key slug: [a-z0-9_-]+, permanent
#         namespace: example.github.io   # no '/' or whitespace
#         name: flash-finished-factory   # no '/' or whitespace
#         label: Boards Flashed (factory)
#         unit: flashes
#     max: 20                    # cardinality guard on counters
#     # No auth, no env vars -- the source is unauthenticated and public.
#
#   tiktok:
#     enabled: true              # every plugin is OFF until explicitly enabled
#     handles:
#       - key: main              # metric-key slug: [a-z0-9_-]+, permanent
#         handle: yourhandle      # without the leading @; letters/digits/./_ only
#         # allow_zero_followers: true  # opt out of the zero-followers guard
#         #                              # (default false) for a fresh account
#     max: 5                     # cardinality guard on handles
#     videos:                    # optional, independent of handles -- views/likes
#       - key: launch_video      # metric-key slug: [a-z0-9_-]+, permanent
#         id: "7123456789012345678"   # the numeric id from the video's URL
#     videos_max: 20             # cardinality guard on videos
#     # No auth -- reads your own public profile/video pages.
#
# dashboard:
#   pinned: []        # up to 6 metric keys for the index strip
#                      # empty = first 4 cumulative metrics

# mqtt:                          # omit this whole block to keep MQTT off
#   host: "192.168.1.x"          # required; no default -- no block, no connection
#   port: 1883
#   username: "ngu"              # optional
#   tls: false                   # true = TLS with system CAs (usually port 8883)
#   discovery_prefix: "homeassistant"
#   topic_prefix: "numbers-go-up"
#   include: []                  # glob patterns on metric_key; empty = everything
#   exclude: []                  # glob patterns on metric_key; checked after include
#   # Password: set the NGU_MQTT_PASSWORD env var, never here.

# milestones:                    # omit this whole block to keep milestones off
#   webhook_url_env: NGU_MILESTONE_WEBHOOK_URL   # env var holding the HA webhook URL
#   rules:
#     - metric: makerworld.profile.design_downloads
#       every: 500                                # fires at 500, 1000, 1500, ...
#     - metric: youtube.channel.main.subscribers
#       at: [1000, 2500, 5000, 10000]              # fires only at these thresholds
#     - metric: makerworld.model.{id}.downloads    # a plugin's METRICS pattern --
#       every: 100                                 # applies to every matching series
#   # Webhook URL: set the env var named above, never here -- it's a credential.
#   # Each threshold fires at most once per series, ever: never on startup,
#   # never again if the value dips and recovers.
"""


class ConfigError(Exception):
    """The config file exists but can't be parsed or used."""


class _Section(BaseModel):
    """Base for every config section: unknown keys fail validation instead
    of being silently ignored (the ``mqqt:``/``milestone:`` typo class)."""

    model_config = ConfigDict(extra="forbid")


class BackupsConfig(_Section):
    keep_daily: StrictInt = 7

    @field_validator("keep_daily")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"must be >= 0, got {value!r}")
        return value


class StorageConfig(_Section):
    path: str
    heartbeat_seconds: StrictInt = 86400
    plugin_runs_retention_days: StrictInt = 30
    backups: BackupsConfig = Field(default_factory=BackupsConfig)

    @field_validator("heartbeat_seconds")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"must be a positive integer, got {value!r}")
        return value

    @field_validator("plugin_runs_retention_days")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"must be >= 0, got {value!r}")
        return value


class PollConfig(_Section):
    default_interval: StrictInt = 1800
    jitter_fraction: float = 0.2

    @field_validator("default_interval")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"must be a positive integer, got {value!r}")
        return value

    @field_validator("jitter_fraction", mode="before")
    @classmethod
    def _reject_non_numeric(cls, value: Any) -> Any:
        # Consumed arithmetically at fire time (interval_seconds * fraction);
        # a quoted "0.2" must fail here, not turn into a ~540-character
        # repeated string the first time a plugin polls. Bools are ints in
        # Python but not a number here, same convention as elsewhere in
        # this module.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(
                f"must be a number, got {value!r} ({type(value).__name__})"
            )
        return float(value)

    @field_validator("jitter_fraction")
    @classmethod
    def _range(cls, value: float) -> float:
        if math.isnan(value) or not 0 <= value < 1:
            raise ValueError(f"must be a finite number in [0, 1), got {value!r}")
        return value


class ServerConfig(_Section):
    external_url: StrictStr | None = None
    # Deprecated: kept only so an existing config with these set still
    # boots (with a warning, logged in load_config -- not here, since a
    # validator shouldn't have side effects) instead of failing outright.
    # The container always listens on 0.0.0.0:8080; the outside port is a
    # docker-compose port-mapping concern.
    host: StrictStr | None = None
    port: StrictInt | None = None

    @field_validator("external_url")
    @classmethod
    def _valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"must be an http(s) URL with a host, got {value!r}")
        return value


class DashboardConfig(_Section):
    pinned: list[StrictStr] = Field(default_factory=list)


class Config(_Section):
    """The top-level config contract. ``extra="forbid"`` here is what makes
    an unknown top-level key (a typo'd section name) fail startup.

    ``mqtt``/``milestones``/``plugins`` stay loosely-typed: their own
    modules validate what's inside them (mqtt.py, milestones.py, and the
    per-plugin contract respectively), so a section moving to a strict
    sub-model here is a separate, contained change, not bundled into this
    one.
    """

    poll: PollConfig = Field(default_factory=PollConfig)
    storage: StorageConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)
    mqtt: dict[str, Any] | None = None
    milestones: dict[str, Any] | None = None


def _format_validation_error(exc: ValidationError) -> str:
    known_top_level = sorted(Config.model_fields)
    parts = []
    for error in exc.errors():
        loc = ".".join(str(segment) for segment in error["loc"])
        msg = error["msg"]
        if error["type"] == "extra_forbidden" and len(error["loc"]) == 1:
            suggestions = difflib.get_close_matches(
                str(error["loc"][0]), known_top_level
            )
            if suggestions:
                msg += f" (did you mean {suggestions[0]!r}?)"
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _defaults(data_dir: str) -> dict[str, Any]:
    return {
        "poll": {
            "default_interval": 1800,
            "jitter_fraction": 0.2,
        },
        "storage": {
            "path": str(Path(data_dir) / "stats.db"),
            "heartbeat_seconds": 86400,
            "plugin_runs_retention_days": 30,
            "backups": {
                "keep_daily": 7,
            },
        },
        "server": {},
        "plugins": {},
        "dashboard": {
            "pinned": [],
        },
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict):
            if not isinstance(value, dict):
                raise ConfigError(
                    f"Config key {key!r} must be a mapping, got {type(value).__name__}"
                )
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _write_example(config_file: Path) -> None:
    example_path = config_file.parent / EXAMPLE_CONFIG_FILENAME
    try:
        example_path.parent.mkdir(parents=True, exist_ok=True)
        example_path.write_text(EXAMPLE_CONFIG)
    except OSError as exc:
        logger.warning(
            "Could not write example config %s: %s; continuing with defaults",
            example_path,
            exc,
        )


def load_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load settings from env-overridable paths, merged over built-in defaults.

    If ``NGU_CONFIG_FILE`` doesn't exist, writes a commented
    ``config.yaml.example`` beside it and returns the defaults untouched —
    zero plugins enabled, per Responsible Use requirement #2.
    """
    env = os.environ if env is None else env

    data_dir = env.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)
    config_file = Path(env.get(ENV_CONFIG_FILE, DEFAULT_CONFIG_FILE))
    plugin_dir = env.get(ENV_PLUGIN_DIR, DEFAULT_PLUGIN_DIR)

    defaults = _defaults(data_dir)

    if not config_file.exists():
        _write_example(config_file)
        user_config: dict[str, Any] = {}
    else:
        try:
            loaded = yaml.safe_load(config_file.read_text(encoding="utf-8-sig"))
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"Could not parse config file {config_file}: {exc}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(
                f"Config file {config_file} is not valid UTF-8: {exc}"
            ) from exc

        if loaded is None:
            user_config = {}
        elif isinstance(loaded, dict):
            user_config = loaded
        else:
            raise ConfigError(
                f"Config file {config_file} must be a YAML mapping at the top "
                f"level, got {type(loaded).__name__}"
            )

    config = _deep_merge(defaults, user_config)

    strict = env.get(ENV_CONFIG_STRICT, "1").strip().lower() not in ("0", "false", "no")
    try:
        validated = Config.model_validate(config)
    except ValidationError as exc:
        message = _format_validation_error(exc)
        if strict:
            raise ConfigError(message) from exc
        logger.warning(
            "Config failed validation (%s) but %s=0 is set; starting anyway "
            "with the config as loaded, unvalidated.",
            message,
            ENV_CONFIG_STRICT,
        )
    else:
        config = validated.model_dump()
        if config["server"]["host"] is not None or config["server"]["port"] is not None:
            logger.warning(
                "server.host/server.port have no effect and are deprecated -- "
                "the container always listens on 0.0.0.0:8080; change the "
                "outside port with docker-compose's port mapping instead "
                '(e.g. "9000:8080"). Set server.external_url if you want '
                'Home Assistant\'s "Visit" link to point at the right host.'
            )

    config["data_dir"] = data_dir
    config["config_file"] = str(config_file)
    config["plugin_dir"] = plugin_dir
    return config
