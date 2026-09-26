"""Offline tests for scripts/merge_source_status.py (#162)."""

from __future__ import annotations

import json

from scripts.merge_source_status import AZURE_IP_CAVEAT_PLUGINS, merge


def _write(tmp_path, plugin, status, checked_at="2026-01-01T00:00:00Z", exit_code=0):
    (tmp_path / f"status-{plugin}.json").write_text(
        json.dumps(
            {
                "plugin": plugin,
                "status": status,
                "checked_at": checked_at,
                "exit_code": exit_code,
            }
        )
    )


def test_merge_combines_every_status_file_sorted_by_plugin(tmp_path):
    _write(tmp_path, "youtube", "ok")
    _write(tmp_path, "abacus", "broken", exit_code=1)

    result = merge(tmp_path)

    assert [s["plugin"] for s in result["sources"]] == ["abacus", "youtube"]
    assert result["sources"][0]["status"] == "broken"
    assert result["sources"][0]["exit_code"] == 1
    assert "generated_at" in result


def test_merge_flags_the_azure_ip_caveat_plugins_only(tmp_path):
    for plugin in ("makerworld", "github", "tiktok", "abacus", "youtube"):
        _write(tmp_path, plugin, "ok")

    result = merge(tmp_path)

    flagged = {s["plugin"] for s in result["sources"] if s["azure_ip_caveat"]}
    assert flagged == set(AZURE_IP_CAVEAT_PLUGINS)


def test_merge_rejects_an_invalid_status(tmp_path):
    _write(tmp_path, "github", "definitely-not-a-real-status")

    try:
        merge(tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an invalid status")


def test_merge_never_includes_a_secret_shaped_field(tmp_path):
    # The merged file must carry only the enum/timestamp/exit-code shape --
    # never free-text captured output, which is exactly where a probe
    # secret could leak (the workflow scrubs it from logs, but the status
    # JSON must never have had it in the first place).
    _write(tmp_path, "makerworld", "broken", exit_code=1)

    result = merge(tmp_path)

    assert set(result["sources"][0]) == {
        "plugin",
        "status",
        "checked_at",
        "exit_code",
        "azure_ip_caveat",
    }
