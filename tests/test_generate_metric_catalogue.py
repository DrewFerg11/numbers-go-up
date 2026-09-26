"""Offline tests for scripts/generate_metric_catalogue.py (#164)."""

from __future__ import annotations

from scripts.generate_metric_catalogue import generate


def test_every_shipped_plugin_appears_including_ones_with_no_prose_page():
    output = generate()

    for plugin in ("abacus", "github", "makerworld", "tiktok", "youtube"):
        assert f"## [{plugin}]" in output, plugin


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
