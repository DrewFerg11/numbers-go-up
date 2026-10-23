"""Offline tests for scripts/check_docs_sources.py (#152)."""

from pathlib import Path

from scripts import check_docs_sources as check

_CONFIG = """\
site_name: t
exclude_docs: |
  requirements.txt
nav:
  - Home: index.md
  - Group:
      - sub/index.md
      - Other: sub/other.md
"""


def _tree(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    (tmp_path / "mkdocs.yml").write_text(_CONFIG + "extra:\n  x: !ENV [A, false]\n")
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    for name in ("index.md", "sub/index.md", "sub/other.md", "requirements.txt"):
        (docs / name).write_text("x")
    for name, text in (extra or {}).items():
        (docs / name).write_text(text)
    return tmp_path


def test_real_repo_passes():
    # Generated pages are gitignored, so a fresh checkout lacks them; the
    # docs build job runs the generators first and checks them for real.
    assert check.check(check.ROOT, generated=check.GENERATED_PAGES) == []


def test_missing_generated_page_is_reported_unless_allowed(tmp_path):
    root = _tree(tmp_path)
    (root / "docs" / "sub" / "other.md").unlink()
    assert check.check(root, generated=frozenset({"sub/other.md"})) == []


def test_clean_tree_passes(tmp_path):
    assert check.check(_tree(tmp_path)) == []


def test_orphan_page_is_reported(tmp_path):
    problems = check.check(_tree(tmp_path, {"orphan.md": "x"}))
    assert problems == ["docs/orphan.md is not in the nav in mkdocs.yml"]


def test_nav_entry_without_a_file_is_reported(tmp_path):
    root = _tree(tmp_path)
    (root / "docs" / "sub" / "other.md").unlink()
    assert check.check(root) == ["nav lists sub/other.md, which is not a docs page"]


def test_published_build_input_is_reported(tmp_path):
    root = _tree(tmp_path)
    site = root / "site"
    site.mkdir()
    (site / "index.html").write_text("x")
    assert check.check(root, site) == []
    (site / "requirements.txt").write_text("x")
    assert check.check(root, site) == [
        "requirements.txt is excluded by exclude_docs but was published"
    ]
