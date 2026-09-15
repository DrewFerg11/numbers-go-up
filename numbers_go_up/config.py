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
        },
        "server": {
            "host": "0.0.0.0",
            "port": 8080,
        },
        "plugins": {},
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
