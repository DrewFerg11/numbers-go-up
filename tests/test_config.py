from pathlib import Path

import pytest
import yaml

from numbers_go_up.config import (
    EXAMPLE_CONFIG,
    EXAMPLE_CONFIG_FILENAME,
    ConfigError,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "NGU_DATA_DIR": str(tmp_path / "data"),
        "NGU_CONFIG_FILE": str(tmp_path / "config" / "config.yaml"),
        "NGU_PLUGIN_DIR": str(tmp_path / "plugins"),
    }
    env.update(overrides)
    return env


def test_repo_root_example_matches_the_template_the_app_writes():
    committed = (REPO_ROOT / "config.yaml.example").read_text()

    assert committed == EXAMPLE_CONFIG


def test_missing_config_file_writes_example_and_uses_defaults(tmp_path):
    env = _env(tmp_path)

    config = load_config(env=env)

    example_path = tmp_path / "config" / EXAMPLE_CONFIG_FILENAME
    assert example_path.exists()
    assert example_path.read_text() == EXAMPLE_CONFIG

    assert config["poll"]["default_interval"] == 1800
    assert config["poll"]["jitter_fraction"] == 0.2
    assert config["storage"]["heartbeat_seconds"] == 86400
    assert config["storage"]["plugin_runs_retention_days"] == 30
    assert config["server"]["host"] == "0.0.0.0"
    assert config["server"]["port"] == 8080
    assert config["plugins"] == {}


def test_generated_example_loads_to_the_same_settings_as_defaults(tmp_path):
    baseline = load_config(env=_env(tmp_path, NGU_DATA_DIR=str(tmp_path / "data")))

    example_path = tmp_path / "config" / EXAMPLE_CONFIG_FILENAME
    renamed = tmp_path / "config2" / "config.yaml"
    renamed.parent.mkdir(parents=True)
    renamed.write_text(example_path.read_text())

    from_example = load_config(
        env=_env(
            tmp_path,
            NGU_CONFIG_FILE=str(renamed),
            NGU_DATA_DIR=str(tmp_path / "data"),
        )
    )

    assert from_example["poll"] == baseline["poll"]
    assert from_example["storage"] == baseline["storage"]
    assert from_example["server"] == baseline["server"]
    assert from_example["plugins"] == baseline["plugins"]


def test_partial_config_merges_over_defaults_instead_of_replacing(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "plugins": {
                    "makerworld": {
                        "enabled": True,
                        "user_id": "12345",
                    }
                }
            }
        )
    )

    config = load_config(env=_env(tmp_path, NGU_CONFIG_FILE=str(config_file)))

    # user-supplied section is present...
    assert config["plugins"]["makerworld"]["enabled"] is True
    # ...and everything else still comes from defaults, not wiped out.
    assert config["poll"]["default_interval"] == 1800
    assert config["storage"]["heartbeat_seconds"] == 86400
    assert config["server"]["port"] == 8080


def test_zero_plugins_enabled_by_default(tmp_path):
    config = load_config(env=_env(tmp_path))

    assert config["plugins"] == {}


def test_every_path_is_env_overridable(tmp_path):
    custom_data_dir = tmp_path / "somewhere-else"
    env = _env(tmp_path, NGU_DATA_DIR=str(custom_data_dir))

    config = load_config(env=env)

    assert config["data_dir"] == str(custom_data_dir)
    assert config["storage"]["path"] == str(custom_data_dir / "stats.db")
    assert config["plugin_dir"] == env["NGU_PLUGIN_DIR"]


def test_malformed_yaml_raises_config_error_naming_file_and_problem(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "config.yaml"
    config_file.write_text("plugins: [this is not: valid: yaml")

    with pytest.raises(ConfigError) as exc_info:
        load_config(env=_env(tmp_path, NGU_CONFIG_FILE=str(config_file)))

    message = str(exc_info.value)
    assert str(config_file) in message


def test_non_mapping_yaml_raises_config_error(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "config.yaml"
    config_file.write_text("- just\n- a\n- list\n")

    with pytest.raises(ConfigError) as exc_info:
        load_config(env=_env(tmp_path, NGU_CONFIG_FILE=str(config_file)))

    assert str(config_file) in str(exc_info.value)


def test_example_config_documents_the_mqtt_block_commented_out():
    assert "# mqtt:" in EXAMPLE_CONFIG
    assert '#   host: "192.168.1.x"' in EXAMPLE_CONFIG
    assert "NGU_MQTT_PASSWORD" in EXAMPLE_CONFIG
    # Commented out -- an example config alone must never turn MQTT on.
    assert "\nmqtt:" not in EXAMPLE_CONFIG


def test_missing_mqtt_block_means_mqtt_is_off_by_default(tmp_path):
    config = load_config(env=_env(tmp_path))

    assert config.get("mqtt") is None


def test_scalar_section_raises_config_error_naming_key(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "config.yaml"
    config_file.write_text("poll: 1800\n")

    with pytest.raises(ConfigError) as exc_info:
        load_config(env=_env(tmp_path, NGU_CONFIG_FILE=str(config_file)))

    assert "poll" in str(exc_info.value)
