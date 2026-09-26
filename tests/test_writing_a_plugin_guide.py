"""Offline tests for docs/plugins/writing-a-plugin.md (#165).

Not a doc-content test in the usual sense: it mechanically checks that
every API name the guide claims exists actually does, and that the
snippets presented as real code are byte-identical to the source they're
quoted from -- exactly the acceptance criterion "every API, helper and
decorator named exists and is spelled as the code spells it".
"""

from __future__ import annotations

from pathlib import Path

GUIDE = (
    Path(__file__).parent.parent / "docs" / "plugins" / "writing-a-plugin.md"
).read_text()


def test_every_named_symbol_exists_where_the_guide_says_it_does():
    import numbers_go_up.plugins as plugins_init
    from numbers_go_up import http, scheduler
    from numbers_go_up.plugins import abacus, makerworld

    assert hasattr(http, "build_client")
    assert hasattr(http, "RateLimited")
    assert hasattr(http, "Blocked")
    assert hasattr(scheduler, "run_plugin_once")
    assert hasattr(plugins_init, "MIN_POLL_INTERVAL_SECONDS")
    assert hasattr(abacus, "_validate_counters")
    assert hasattr(abacus, "_validate_base_url")
    assert hasattr(abacus, "_validate_value")
    assert hasattr(abacus, "_last_known_values")
    assert hasattr(abacus, "_DEFAULT_MAX_COUNTERS")
    assert hasattr(makerworld, "_warned_missing_model_ids")


def test_the_quoted_metrics_dict_matches_the_real_one_exactly():
    from numbers_go_up.plugins.abacus import METRICS

    quoted = """METRICS = {
    "abacus.counter.{key}.value": {
        "kind": "cumulative",  # Abacus counters only rise in normal operation
        "label": "Abacus Counter",
        "unit": "count",
        "icon": "mdi:counter",
    },
}"""
    assert quoted in GUIDE

    namespace: dict = {}
    exec(compile(quoted, "<guide>", "exec"), namespace)  # noqa: S102
    assert namespace["METRICS"] == METRICS


def test_the_quoted_live_test_matches_the_real_one_exactly():
    real = (Path(__file__).parent / "test_abacus_plugin.py").read_text()
    start = real.index("@pytest.mark.live\ndef test_live_abacus():")
    quoted_real = real[start:].strip()

    assert quoted_real in GUIDE


def test_the_collect_signature_matches_the_contract():
    import inspect

    from numbers_go_up.plugins import abacus

    sig = str(inspect.signature(abacus.collect))
    assert sig == "(config: dict, http) -> dict[str, int | float | dict]"
    assert "def collect(config: dict, http) -> dict[str, int | float | dict]:" in GUIDE


def test_canary_matrix_snippet_matches_the_real_matrix_entry():
    workflow = (
        Path(__file__).parent.parent / ".github" / "workflows" / "sources-canary.yml"
    ).read_text()

    assert "- plugin: abacus" in workflow
    assert "secret_name: NGU_CANARY_ABACUS_NAMESPACE" in workflow
    assert "extra_secret_name: NGU_CANARY_ABACUS_KEY" in workflow
    for line in (
        "- plugin: abacus",
        "secret_name: NGU_CANARY_ABACUS_NAMESPACE",
        "extra_secret_name: NGU_CANARY_ABACUS_KEY",
    ):
        assert line in GUIDE
