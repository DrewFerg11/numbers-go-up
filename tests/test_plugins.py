import logging
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from numbers_go_up import plugins

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "plugins"


def _config(plugin_dir=None, plugins_config=None, poll=None):
    return {
        "plugin_dir": plugin_dir,
        "plugins": plugins_config or {},
        "poll": poll or {"default_interval": 1800},
    }


def _user_dir_with_no_poll_plugin(tmp_path):
    """A user plugin with no POLL_INTERVAL_SECONDS, so the configured
    ``poll.default_interval`` is the only interval source available."""
    user_dir = tmp_path / "user-plugins"
    user_dir.mkdir()
    (user_dir / "no_poll.py").write_text(
        'METRICS = {"no_poll.thing.count": '
        '{"kind": "gauge", "label": "Thing", "unit": ""}}\n'
        "def collect(config, http): raise NotImplementedError\n"
    )
    return user_dir


class TestLoadPluginFromPath:
    def test_loads_a_valid_module(self):
        module = plugins.load_plugin_from_path(FIXTURES_DIR / "valid.py")

        assert module is not None
        assert module.METRICS == {
            "valid.thing.count": {
                "kind": "cumulative",
                "label": "Thing Count",
                "unit": "things",
                "icon": "mdi:counter",
            }
        }

    def test_a_module_that_raises_on_import_is_logged_and_skipped(self, caplog):
        with caplog.at_level("ERROR"):
            module = plugins.load_plugin_from_path(FIXTURES_DIR / "broken_import.py")

        assert module is None


class TestValidatePluginContract:
    def test_valid_plugin_passes(self):
        module = plugins.load_plugin_from_path(FIXTURES_DIR / "valid.py")

        metrics = plugins.validate_plugin_contract("valid", module)

        assert metrics == module.METRICS

    def test_missing_collect_is_rejected(self, caplog):
        module = plugins.load_plugin_from_path(FIXTURES_DIR / "no_collect.py")

        with caplog.at_level("WARNING"):
            metrics = plugins.validate_plugin_contract("no_collect", module)

        assert metrics is None

    def test_bad_kind_is_rejected(self, caplog):
        module = plugins.load_plugin_from_path(FIXTURES_DIR / "bad_kind.py")

        with caplog.at_level("WARNING"):
            metrics = plugins.validate_plugin_contract("bad_kind", module)

        assert metrics is None

    def test_key_not_prefixed_with_plugin_name_is_rejected(self, caplog):
        module = plugins.load_plugin_from_path(FIXTURES_DIR / "bad_prefix.py")

        with caplog.at_level("WARNING"):
            metrics = plugins.validate_plugin_contract("bad_prefix", module)

        assert metrics is None

    def test_missing_metrics_is_rejected(self, caplog):
        module = ModuleType("no_metrics")
        module.collect = lambda config, http: {}

        with caplog.at_level("WARNING"):
            metrics = plugins.validate_plugin_contract("no_metrics", module)

        assert metrics is None

    def test_pattern_key_with_one_whole_segment_placeholder_is_accepted(self):
        module = ModuleType("mw")
        module.collect = lambda config, http: {}
        module.METRICS = {
            "mw.model.{id}.downloads": {
                "kind": "cumulative",
                "label": "Downloads",
                "unit": "downloads",
            }
        }

        metrics = plugins.validate_plugin_contract("mw", module)

        assert metrics == module.METRICS

    def test_pattern_with_two_placeholders_is_rejected(self, caplog):
        module = ModuleType("mw")
        module.collect = lambda config, http: {}
        module.METRICS = {
            "mw.{a}.{b}.count": {"kind": "gauge", "label": "X", "unit": ""}
        }

        with caplog.at_level("WARNING"):
            metrics = plugins.validate_plugin_contract("mw", module)

        assert metrics is None

    def test_placeholder_not_occupying_a_whole_segment_is_rejected(self, caplog):
        module = ModuleType("mw")
        module.collect = lambda config, http: {}
        module.METRICS = {
            "mw.model{id}.count": {"kind": "gauge", "label": "X", "unit": ""}
        }

        with caplog.at_level("WARNING"):
            metrics = plugins.validate_plugin_contract("mw", module)

        assert metrics is None

    def test_same_shape_patterns_differing_only_in_final_segment_do_not_overlap(
        self, caplog
    ):
        module = ModuleType("mw")
        module.collect = lambda config, http: {}
        module.METRICS = {
            "mw.model.{id}.downloads": {
                "kind": "cumulative",
                "label": "Downloads",
                "unit": "downloads",
            },
            "mw.model.{id}.likes": {
                "kind": "gauge",
                "label": "Likes",
                "unit": "likes",
            },
        }

        # Same shape, different metric name at the end -- these don't
        # overlap (the last segment always differs), so this is the
        # control case proving the check isn't just rejecting every
        # multi-pattern plugin.
        metrics = plugins.validate_plugin_contract("mw", module)

        assert metrics == module.METRICS

    def test_two_patterns_that_could_match_the_same_key_are_rejected(self, caplog):
        module = ModuleType("mw")
        module.collect = lambda config, http: {}
        module.METRICS = {
            "mw.a.{id}.count": {"kind": "gauge", "label": "A", "unit": ""},
            # Each pattern has exactly one placeholder (so the earlier
            # single-placeholder check passes both individually), but at
            # different positions. A placeholder can match another
            # pattern's literal segment ("a"/"b" both satisfy
            # [a-z0-9_-]+), so "mw.a.b.count" matches this one with
            # id="b" *and* the one below with other="a" -- which one wins
            # is undefined (dict order).
            "mw.{other}.b.count": {"kind": "gauge", "label": "B", "unit": ""},
        }

        with caplog.at_level("WARNING"):
            metrics = plugins.validate_plugin_contract("mw", module)

        assert metrics is None
        assert "overlap" in caplog.text

    def test_patterns_with_different_literal_segments_do_not_overlap(self):
        module = ModuleType("mw")
        module.collect = lambda config, http: {}
        module.METRICS = {
            "mw.a.{id}.count": {"kind": "gauge", "label": "A", "unit": ""},
            "mw.b.{id}.count": {"kind": "gauge", "label": "B", "unit": ""},
        }

        metrics = plugins.validate_plugin_contract("mw", module)

        assert metrics == module.METRICS

    def test_patterns_with_different_segment_counts_do_not_overlap(self):
        module = ModuleType("mw")
        module.collect = lambda config, http: {}
        module.METRICS = {
            "mw.{id}.count": {"kind": "gauge", "label": "A", "unit": ""},
            "mw.{id}.sub.count": {"kind": "gauge", "label": "B", "unit": ""},
        }

        metrics = plugins.validate_plugin_contract("mw", module)

        assert metrics == module.METRICS


class TestDiscoverPlugins:
    def test_discovers_builtin_plugins(self):
        config = _config()

        loaded = plugins.discover_plugins(
            {**config, "plugins": {"valid": {"enabled": True}}},
            builtin_dir=FIXTURES_DIR,
        )

        assert [p.name for p in loaded] == ["valid"]

    def test_zero_plugins_enabled_with_default_config(self):
        loaded = plugins.discover_plugins(_config(), builtin_dir=FIXTURES_DIR)

        assert loaded == []

    def test_invalid_plugins_are_skipped_without_stopping_discovery(self):
        config = _config(
            plugins_config={
                "valid": {"enabled": True},
                "no_collect": {"enabled": True},
                "bad_kind": {"enabled": True},
                "bad_prefix": {"enabled": True},
                "broken_import": {"enabled": True},
            }
        )

        loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert [p.name for p in loaded] == ["valid"]

    def test_underscore_prefixed_files_are_never_discovered(self, caplog):
        # Also proves discovery never even attempts to import it: the
        # fixture file has invalid syntax and would raise if imported.
        config = _config(plugins_config={"underscored": {"enabled": True}})

        with caplog.at_level(logging.WARNING):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert [p.name for p in loaded] == []
        # Discovery must not even attempt to import underscore-prefixed
        # files: nothing about _underscored was logged (an attempted
        # import of its invalid syntax would log an error).
        assert "_underscored" not in caplog.text
        # Direct proof: the loader was called for every non-underscore
        # fixture file but never for _underscored.py.
        with patch.object(
            plugins, "load_plugin_from_path", wraps=plugins.load_plugin_from_path
        ) as loader:
            plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        imported = {call.args[0].stem for call in loader.call_args_list}
        assert "_underscored" not in imported
        assert "valid" in imported

    def test_file_whose_name_is_not_an_identifier_is_skipped(self, tmp_path, caplog):
        # The stem becomes the plugin name and the prefix of its published
        # metric keys (Home-Assistant-facing), so discovery rejects stems
        # that are not lowercase identifiers before importing anything.
        user_dir = tmp_path / "user-plugins"
        user_dir.mkdir()
        (user_dir / "bad-name.py").write_text(
            'METRICS = {"bad-name.thing": '
            '{"kind": "gauge", "label": "Thing", "unit": ""}}\n'
            "def collect(config, http): return {}\n"
        )
        (user_dir / "2024 stats.py").write_text("raise AssertionError\n")
        config = _config(
            plugin_dir=str(user_dir),
            plugins_config={
                "valid": {"enabled": True},
                "bad-name": {"enabled": True},
                "2024 stats": {"enabled": True},
            },
        )

        with caplog.at_level(logging.WARNING):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        # Neither bad file was imported (2024 stats.py raises on import),
        # both were logged and skipped, and the valid fixture still loads.
        assert [p.name for p in loaded] == ["valid"]
        assert "bad-name.py" in caplog.text
        assert "2024 stats.py" in caplog.text

    def test_missing_user_plugin_dir_is_fine(self, tmp_path):
        config = _config(
            plugin_dir=str(tmp_path / "does-not-exist"),
            plugins_config={"valid": {"enabled": True}},
        )

        loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert [p.name for p in loaded] == ["valid"]

    def test_empty_user_plugin_dir_is_fine(self, tmp_path):
        user_dir = tmp_path / "user-plugins"
        user_dir.mkdir()
        config = _config(plugin_dir=str(user_dir))

        loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded == []

    def test_user_plugin_replaces_a_builtin_of_the_same_name(self, tmp_path):
        user_dir = tmp_path / "user-plugins"
        user_dir.mkdir()
        (user_dir / "valid.py").write_text(
            'METRICS = {"valid.override.count": '
            '{"kind": "gauge", "label": "Override", "unit": ""}}\n'
            "def collect(config, http): raise NotImplementedError\n"
        )
        config = _config(
            plugin_dir=str(user_dir), plugins_config={"valid": {"enabled": True}}
        )

        loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert len(loaded) == 1
        assert loaded[0].source == "user"
        assert "valid.override.count" in loaded[0].metrics

    def test_shadowing_a_builtin_logs_a_warning(self, tmp_path, caplog):
        user_dir = tmp_path / "user-plugins"
        user_dir.mkdir()
        (user_dir / "valid.py").write_text(
            'METRICS = {"valid.override.count": '
            '{"kind": "gauge", "label": "Override", "unit": ""}}\n'
            "def collect(config, http): raise NotImplementedError\n"
        )
        config = _config(
            plugin_dir=str(user_dir), plugins_config={"valid": {"enabled": True}}
        )

        with caplog.at_level(logging.WARNING):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert [p.name for p in loaded] == ["valid"]
        assert loaded[0].source == "user"
        assert "replaces the built-in" in caplog.text

    def test_enabled_plugin_gets_its_own_config_section(self):
        config = _config(
            plugins_config={"valid": {"enabled": True, "poll_interval": 900}}
        )

        loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded[0].config == {"enabled": True, "poll_interval": 900}
        assert loaded[0].interval_seconds == 900

    def test_interval_resolution_falls_back_to_module_then_default(self):
        # valid.py sets POLL_INTERVAL_SECONDS = 1800 and no poll_interval
        # override is configured.
        config = _config(plugins_config={"valid": {"enabled": True}})

        loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded[0].interval_seconds == 1800

    def test_interval_below_the_five_minute_floor_is_raised(self, caplog):
        config = _config(
            plugins_config={"valid": {"enabled": True, "poll_interval": 60}}
        )

        with caplog.at_level("WARNING"):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded[0].interval_seconds == 300

    def test_a_configured_poll_interval_of_zero_is_raised_to_the_floor(self, caplog):
        # 0 is falsy, so the previous or-chain swallowed it into the
        # module/default interval; it must reach the floor check instead.
        config = _config(
            plugins_config={"valid": {"enabled": True, "poll_interval": 0}}
        )

        with caplog.at_level(logging.WARNING):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded[0].interval_seconds == 300
        assert "below the" in caplog.text

    def test_a_configured_default_interval_of_zero_is_raised_to_the_floor(
        self, tmp_path, caplog
    ):
        # Needs a plugin without POLL_INTERVAL_SECONDS so the configured
        # default is the only interval source; with the valid fixture the
        # module interval would win before the default is consulted.
        user_dir = _user_dir_with_no_poll_plugin(tmp_path)
        config = _config(
            plugin_dir=str(user_dir),
            poll={"default_interval": 0},
            plugins_config={"no_poll": {"enabled": True}},
        )

        with caplog.at_level(logging.WARNING):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded[0].interval_seconds == 300
        assert "below the" in caplog.text

    def test_a_non_integer_poll_interval_falls_back_to_the_default(
        self, tmp_path, caplog
    ):
        # "30m" is an easy YAML quoting slip; without the type guard the
        # floor comparison raises TypeError and kills discovery of
        # every plugin.
        user_dir = _user_dir_with_no_poll_plugin(tmp_path)
        config = _config(
            plugin_dir=str(user_dir),
            plugins_config={
                "no_poll": {"enabled": True, "poll_interval": "30m"},
                "valid": {"enabled": True},
            },
        )

        with caplog.at_level(logging.WARNING):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert [p.name for p in loaded] == ["no_poll", "valid"]
        assert loaded[0].interval_seconds == 1800
        assert "is not an integer" in caplog.text

    def test_a_non_integer_default_interval_falls_back_to_the_floor(
        self, tmp_path, caplog
    ):
        user_dir = _user_dir_with_no_poll_plugin(tmp_path)
        config = _config(
            plugin_dir=str(user_dir),
            poll={"default_interval": "30m"},
            plugins_config={"no_poll": {"enabled": True}},
        )

        with caplog.at_level(logging.WARNING):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded[0].interval_seconds == 300
        assert "is not an integer" in caplog.text

    def test_a_boolean_poll_interval_falls_back_to_the_default(self, caplog):
        # True is an int subclass in Python but "poll_interval: true" is a
        # config mistake, not a number of seconds.
        config = _config(
            plugins_config={"valid": {"enabled": True, "poll_interval": True}}
        )

        with caplog.at_level(logging.WARNING):
            loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded[0].interval_seconds == 1800
        assert "is not an integer" in caplog.text

    def test_disabled_plugin_is_discovered_but_not_scheduled(self):
        config = _config(plugins_config={"valid": {"enabled": False}})

        loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded == []

    def test_default_builtin_dir_finds_the_real_template_and_skips_it(self):
        # No builtin_dir override: exercises the real numbers_go_up/plugins
        # package directory, proving _template.py is discovered-and-skipped
        # (found by the glob, then skipped for its underscore prefix)
        # rather than erroring or being scheduled.
        loaded = plugins.discover_plugins(_config())

        assert loaded == []


class TestNonMappingPluginConfigEntry:
    # _deep_merge only type-checks the top-level keys, so a per-plugin
    # entry that isn't a mapping (e.g. plugins: {ticker: "on"} — a quoted
    # value or a flat string) reaches discover_plugins untouched. The
    # loader then calls .get("enabled") on a str and raises AttributeError
    # inside discover_plugins — and since the FastAPI lifespan calls
    # discover_plugins on startup (this PR), one malformed entry refused
    # the whole service startup. A broken entry must degrade to
    # "plugin disabled" instead, the same behavior the issue specifies
    # for broken plugins.

    @staticmethod
    def _user_dir_with_ticker_plugin(tmp_path):
        # A real contract-passing plugin so the crash path (the config
        # lookup for a discovered module) is genuinely reached.
        user_dir = tmp_path / "user-plugins"
        user_dir.mkdir()
        (user_dir / "ticker.py").write_text(
            'METRICS = {"ticker.thing.count": '
            '{"kind": "gauge", "label": "Thing", "unit": ""}}\n'
            "def collect(config, http): return {}\n"
        )
        return user_dir

    def test_a_non_mapping_entry_degrades_to_plugin_disabled(self, tmp_path, caplog):
        # Before the guard this raised AttributeError: 'str' object has no
        # attribute 'get' — aborting discovery inside the lifespan and
        # refusing the whole service startup.
        user_dir = self._user_dir_with_ticker_plugin(tmp_path)
        config = _config(
            plugin_dir=str(user_dir),
            plugins_config={"ticker": "on"},
        )

        loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert loaded == []
        # Read the captured records directly instead of a
        # caplog.at_level(...) block: several pre-existing tests in this
        # file already use that exact two-line shape, and a near-duplicate
        # block here invites transcription mix-ups on later edits.
        assert any(
            "must be a mapping" in record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        )

    def test_other_plugins_still_load_when_one_entry_is_a_non_mapping(
        self, tmp_path, caplog
    ):
        # A malformed entry must not stop discovery of every other plugin
        # (the same containment the non-integer poll_interval guard gives).
        user_dir = self._user_dir_with_ticker_plugin(tmp_path)
        config = _config(
            plugin_dir=str(user_dir),
            plugins_config={
                "ticker": "on",
                "valid": {"enabled": True},
            },
        )

        still_loaded = plugins.discover_plugins(config, builtin_dir=FIXTURES_DIR)

        assert [p.name for p in still_loaded] == ["valid"]
        assert any(
            "must be a mapping" in record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        )


class TestResolveMetric:
    METRICS = {
        "mw.profile.likes": {"kind": "gauge", "label": "Likes", "unit": "likes"},
        "mw.model.{id}.downloads": {
            "kind": "cumulative",
            "label": "Downloads",
            "unit": "downloads",
        },
    }

    def test_exact_key_matches_itself(self):
        result = plugins.resolve_metric("mw.profile.likes", self.METRICS)

        assert result == ("mw.profile.likes", self.METRICS["mw.profile.likes"])

    def test_pattern_key_matches_a_concrete_subject(self):
        result = plugins.resolve_metric("mw.model.3070072.downloads", self.METRICS)

        assert result == (
            "mw.model.{id}.downloads",
            self.METRICS["mw.model.{id}.downloads"],
        )

    def test_placeholder_value_outside_allowed_charset_does_not_match(self):
        assert (
            plugins.resolve_metric("mw.model.HasCaps.downloads", self.METRICS) is None
        )
        assert plugins.resolve_metric("mw.model..downloads", self.METRICS) is None

    def test_unknown_key_matches_nothing(self):
        assert plugins.resolve_metric("mw.unknown.thing", self.METRICS) is None

    def test_another_plugins_key_never_matches(self):
        # Isolation: a key from a different plugin's namespace, even one
        # shaped like it could match this plugin's pattern, must never
        # resolve against another plugin's own METRICS dict.
        assert plugins.resolve_metric("other.model.123.downloads", self.METRICS) is None
