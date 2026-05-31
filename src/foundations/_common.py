"""Shared helpers for foundation scrapers.

Kept tiny on purpose — every scraper just needs: a banner, atomic CSV
writes, a github slug extractor, and a consistent User-Agent.
"""

import csv
import os
import re
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "sources" / "foundations"
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"

# Browser-like UA — some foundation sites (numfocus, sfc) block default httpx UA.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

GITHUB_RE = re.compile(r"github\.com[:/]([^/\s#?]+)/([^/\s#?.]+)", re.IGNORECASE)

# Top-level paths under github.com that are NOT user/repo slugs — keep the
# extractor from emitting nonsense like `orgs/numfocus` when a foundation
# links to its github org's listing or a marketplace page.
_GITHUB_NON_REPO_OWNERS = {
    "orgs", "users", "topics", "marketplace", "settings", "apps",
    "sponsors", "search", "explore", "trending", "collections",
    "events", "issues", "pulls", "notifications", "about", "pricing",
    "features", "security", "enterprise", "team", "customer-stories",
}

# Package-registry URL patterns we recognise. These produce
# (ecosystem, package) pairs we can join against `data/value/value.csv`.
_PACKAGE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # npm: https://www.npmjs.com/package/foo  OR  /package/@scope/bar
    (re.compile(r"npmjs\.com/package/(@[a-z0-9._-]+/[a-z0-9._-]+|[a-z0-9._-]+)",
                re.IGNORECASE), "npm"),
    # PyPI: https://pypi.org/project/foo/  (and legacy pypi.python.org)
    (re.compile(r"pypi\.(?:org|python\.org)/(?:project|pypi)/([a-z0-9._-]+)",
                re.IGNORECASE), "pypi"),
    # crates.io: https://crates.io/crates/foo
    (re.compile(r"crates\.io/crates/([a-z0-9._-]+)", re.IGNORECASE), "crates"),
    # Homebrew (proxy for our `cpp` ecosystem)
    (re.compile(r"formulae\.brew\.sh/formula/([a-z0-9._@+-]+)",
                re.IGNORECASE), "cpp"),
]

console = Console()


def github_slug(*texts: str) -> str:
    """Extract `owner/name` from any of the provided URLs / text snippets.

    First match wins. Owner names that look like github subpages
    (`orgs`, `marketplace`, …) are skipped so that a link to a github org
    listing doesn't produce a fake repo slug.
    """
    for text in texts:
        if not text:
            continue
        for m in GITHUB_RE.finditer(text):
            owner, repo = m.group(1), m.group(2)
            if owner.lower() in _GITHUB_NON_REPO_OWNERS:
                continue
            if repo.endswith(".git"):
                repo = repo[:-4]
            return f"{owner}/{repo}"
    return ""


def extract_package(*texts: str) -> tuple[str, str]:
    """Find the first npm/pypi/crates/cpp package URL in any of `texts`.

    Returns `(ecosystem, package)`, or `("", "")` if none match. Used to
    join foundation projects against `data/value/value.csv` even when the
    project's homepage is just a github URL (so its `domain` is useless).
    """
    for text in texts:
        if not text:
            continue
        for rx, eco in _PACKAGE_PATTERNS:
            m = rx.search(text)
            if m:
                pkg = m.group(1).rstrip("/.,;:)>").lower()
                return eco, pkg
    return "", ""


def out_path(slug: str) -> Path:
    """Standard output path: data/sources/foundations/{slug}/projects.csv."""
    p = DATA_DIR / slug / "projects.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def banner(slug: str, title: str, subtitle: str) -> None:
    console.rule(f"[bold cyan]foundations/{slug} — {title}")
    console.print(f"  {subtitle}")
    console.print(f"  Started : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]\n")


def summary_table(slug: str, rows: list[dict], path: Path,
                  group_by: str | None = None) -> None:
    """Print a small rich summary after each scraper run."""
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Total projects", f"{len(rows):,}")
    if rows and "github_repo" in rows[0]:
        with_gh = sum(1 for r in rows if r.get("github_repo"))
        pct = 100 * with_gh / max(len(rows), 1)
        tbl.add_row("With github slug", f"{with_gh:,} ({pct:.1f}%)")
    if rows and "package" in rows[0]:
        with_pkg = sum(1 for r in rows if r.get("package"))
        pct = 100 * with_pkg / max(len(rows), 1)
        tbl.add_row("With package", f"{with_pkg:,} ({pct:.1f}%)")
        eco_counts: dict[str, int] = {}
        for r in rows:
            e = r.get("ecosystem") or ""
            if e:
                eco_counts[e] = eco_counts.get(e, 0) + 1
        for e, c in sorted(eco_counts.items(), key=lambda kv: kv[1], reverse=True):
            tbl.add_row(f"  ecosystem={e}", f"{c:,}")
    if group_by and rows and group_by in rows[0]:
        counts: dict[str, int] = {}
        for r in rows:
            k = r.get(group_by) or "(none)"
            counts[k] = counts.get(k, 0) + 1
        for k, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]:
            tbl.add_row(f"  {group_by}={k}", f"{c:,}")
    console.print(tbl)
    console.print(f"  → wrote [cyan]{path}[/cyan]")
