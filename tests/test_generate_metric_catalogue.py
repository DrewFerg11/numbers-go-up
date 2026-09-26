"""Offline tests for scripts/generate_metric_catalogue.py (#164)."""

from __future__ import annotations

from numbers_go_up.plugins import discover
from scripts.generate_metric_catalogue import generate


def test_every_shipped_plugin_appears_including_ones_with_no_prose_page():
    output = generate()

    for plugin in ("abacus", "github", "makerworld", "tiktok", "youtube"):
        assert f"## [{plugin}]" in output, plugin


def test_every_declared_metric_renders_as_a_row_with_its_real_kind_and_unit():
    # The acceptance criterion is "every metric every shipped plugin
    # declares appears, with its real kind and unit" -- checked against
    # plugin.metrics directly, not just that some table exists, so a
    # dropped row or a kind/unit a generator regression mangles fails
    # this test instead of silently shipping.
    output = generate()

    for plugin in discover({}):
        section = output.split(f"## [{plugin.name}]", 1)[1].split("\n## ", 1)[0]
        rows = [line for line in section.splitlines() if line.startswith("| `")]
        assert len(rows) == len(plugin.metrics), plugin.name

        for key, meta in plugin.metrics.items():
            expected = f"| `{key}` |"
            matching = [row for row in rows if row.startswith(expected)]
            assert len(matching) == 1, f"{plugin.name}.{key}"
            assert f"| {meta['kind']} |" in matching[0], f"{plugin.name}.{key}"
            assert (meta.get("unit") or "") in matching[0], f"{plugin.name}.{key}"


def test_pattern_keys_are_visibly_distinguished_from_exact_keys():
    output = generate()

    assert "| `makerworld.model.{id}.downloads` | Yes |" in output
    assert "| `makerworld.profile.likes` | No |" in output


def test_plugins_with_a_prose_page_link_to_it_others_link_to_source():
    output = generate()

    assert "## [github](github.md)" in output
    assert "## [makerworld](makerworld.md)" in output
    assert "## [youtube](youtube.md)" in output
    assert (
        "## [abacus](https://github.com/DrewFerg11/numbers-go-up/blob/main/"
        "numbers_go_up/plugins/abacus.py)" in output
    )


def test_generation_needs_no_config_database_or_network():
    # Same invariant as the config reference generator: pure import + a
    # discover({}) pass, nothing that touches the filesystem beyond the
    # plugin source files themselves or a socket.
    generate()
    generate()
