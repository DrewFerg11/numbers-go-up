"""Configuration loading: env vars, YAML file, and defaults.

This is the only module that reads an ``NGU_*`` environment variable or
resolves a filesystem path directly. Everything else — storage, plugins,
the scheduler — gets its settings from the dict this module returns.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

ENV_DATA_DIR = "NGU_DATA_DIR"
ENV_CONFIG_FILE = "NGU_CONFIG_FILE"
ENV_PLUGIN_DIR = "NGU_PLUGIN_DIR"

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
#   host: 0.0.0.0
#   port: 8080
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
#     # Required: set the NGU_YOUTUBE_API_KEY env var, never here.
#
#   tiktok:
#     enabled: true              # every plugin is OFF until explicitly enabled
#     handles:
#       - key: main              # metric-key slug: [a-z0-9_-]+, permanent
#         handle: yourhandle      # without the leading @; letters/digits/./_ only
#     max: 5                     # cardinality guard on handles
#     # No auth -- reads your own public profile page.
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
        "server": {
            "host": "0.0.0.0",
            "port": 8080,
        },
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
    config["data_dir"] = data_dir
    config["config_file"] = str(config_file)
    config["plugin_dir"] = plugin_dir
    return config
