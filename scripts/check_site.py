"""Post-build sanity checks on the built docs site (issue #167).

Run after `mkdocs build --strict`. Strict mode proves the Markdown is sound;
this proves the properties of the *output* that no warning covers, each of
which broke, or nearly broke, while the polish work was being built:

- sitemap.xml lists real pages under the canonical site_url, and not the 404
- the root 404.html is the custom page, and its links are root-absolute
  (GitHub Pages serves that one file for a bad path at any depth, including
  inside preview/<branch>/, so a relative link would resolve against the bad
  address)
- no page loads a third-party font or makes the repo widget's GitHub API
  call: the docs site follows the app's own no-third-party-requests rule

Usage: python scripts/check_site.py [site_dir]
"""

import re
import sys
from pathlib import Path

SITE_URL = "https://drewferg11.github.io/numbers-go-up/"

# Hosts the default Material theme loads fonts from, which this site turned
# off. Matched as a loaded resource (href/src/url()), not as text, because
# plugin pages legitimately name hosts in prose. The other default third-party
# call, the header repo widget's GitHub API request, is caught by its
# `data-md-component="source"` hook below.
_FORBIDDEN_RESOURCE = re.compile(
    r"""(?:href|src)=["']https?://(?:fonts\.googleapis\.com|fonts\.gstatic\.com)"""
    r"""|url\(["']?https?://(?:fonts\.googleapis\.com|fonts\.gstatic\.com)"""
)


def check(site: Path) -> list[str]:
    problems: list[str] = []

    locs = re.findall(r"<loc>([^<]+)</loc>", (site / "sitemap.xml").read_text())
    if not locs:
        problems.append("sitemap.xml lists no pages")
    for loc in locs:
        if not loc.startswith(SITE_URL):
            problems.append(f"sitemap URL is not under the canonical site_url: {loc}")
        if "404" in loc:
            problems.append(f"sitemap advertises the error page: {loc}")

    page = (site / "404.html").read_text()
    article = re.search(r"<article.*?</article>", page, re.S)
    if article is None or "Page not found" not in article.group(0):
        problems.append("404.html is not the custom 404 page")
    else:
        for href in re.findall(r'href="([^"]*)"', article.group(0)):
            if not href.startswith("/"):
                problems.append(f"404.html link is not root-absolute: {href}")

    for html in site.rglob("*.html"):
        # The demo is exported, not authored, and is covered by its own tests.
        if "demo" in html.relative_to(site).parts[:1]:
            continue
        text = html.read_text(errors="ignore")
        if _FORBIDDEN_RESOURCE.search(text):
            problems.append(f"{html.relative_to(site)} loads a third-party font")
        if 'data-md-component="source"' in text:
            problems.append(f"{html.relative_to(site)} has the GitHub-API repo widget")
    return problems


def main(argv: list[str]) -> int:
    site = Path(argv[1] if len(argv) > 1 else "site")
    problems = check(site)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print(f"Site checks passed for {site}/")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
