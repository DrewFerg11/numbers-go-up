"""Docs-source checks that do not depend on the site generator (issue #152).

mkdocs.yml is read by both Material for MkDocs and Zensical, but the two
disagree about what a build enforces. Zensical ignores
`validation.nav.omitted_files`, so a page missing from the nav passes its
`--strict` build (checked on 0.0.72). Rather than lean on whichever
generator is current, this script checks two properties itself:

- every docs/**/*.md is listed in the nav, and every nav page exists
- no file matched by `exclude_docs` (docs/requirements.txt, a build input)
  was published into the built site

Run before the build for the first check and after it, with the site
directory, for the second.

Usage: python scripts/check_docs_sources.py [site_dir]
"""

import fnmatch
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class _Loader(yaml.SafeLoader):
    """SafeLoader that tolerates MkDocs' custom tags (`!ENV` and friends)."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _nav_pages(nav: object) -> list[str]:
    """Every page path named anywhere in a (possibly nested) nav."""
    if isinstance(nav, str):
        return [nav] if nav.endswith(".md") else []
    if isinstance(nav, list):
        return [page for item in nav for page in _nav_pages(item)]
    if isinstance(nav, dict):
        return [page for value in nav.values() for page in _nav_pages(value)]
    return []


def _excluded(rel: str, patterns: list[str]) -> bool:
    """Gitignore-style subset: a pattern without `/` matches any basename."""
    for pattern in patterns:
        if "/" in pattern.strip("/"):
            if fnmatch.fnmatch(rel, pattern.strip("/")):
                return True
        elif fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pattern):
            return True
    return False


def check(root: Path, site: Path | None = None) -> list[str]:
    config = yaml.load((root / "mkdocs.yml").read_text(), Loader=_Loader)
    docs = root / config.get("docs_dir", "docs")
    patterns = [
        line.strip()
        for line in (config.get("exclude_docs") or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    problems: list[str] = []

    in_nav = set(_nav_pages(config.get("nav")))
    on_disk = {
        p.relative_to(docs).as_posix()
        for p in docs.rglob("*.md")
        if not _excluded(p.relative_to(docs).as_posix(), patterns)
    }
    for page in sorted(on_disk - in_nav):
        problems.append(f"docs/{page} is not in the nav in mkdocs.yml")
    for page in sorted(in_nav - on_disk):
        problems.append(f"nav lists {page}, which is not a docs page")

    if site is not None:
        for path in sorted(site.rglob("*")):
            rel = path.relative_to(site).as_posix()
            if path.is_file() and _excluded(rel, patterns):
                problems.append(f"{rel} is excluded by exclude_docs but was published")
    return problems


def main(argv: list[str]) -> int:
    site = Path(argv[1]) if len(argv) > 1 else None
    problems = check(ROOT, site)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print("Docs source checks passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
