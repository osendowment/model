"""Shared helpers for foundation scrapers.

Kept tiny on purpose — every scraper just needs: a banner, atomic CSV
writes, a github slug extractor, and a consistent User-Agent.

Auditability + idempotency live here too, so all 8 scrapers inherit them:

- Every `projects.csv` carries a `fetched_at` (UTC ISO, seconds) column,
  stamped by `write_projects`.
- `write_projects` enforces a MIN-ROW GUARD: a fresh scrape may not overwrite
  an existing good file if it produced 0 rows (or fewer than `MIN_ROW_FRACTION`
  of the existing count) — a blocked / partial scrape can never clobber data.
- `parse_scraper_args` + `is_output_fresh` gate each scraper on the shared
  funding TTL (`FUNDING_TTL_DAYS`): a re-run inside the window fetches nothing
  and rewrites nothing.
"""

import argparse
import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.freshness import FUNDING_TTL_DAYS, file_is_fresh

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "sources" / "funding"
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"

# Audit column stamped onto every scraper's projects.csv.
FETCHED_AT_COL = "fetched_at"

# A fresh scrape must produce at least this fraction of the existing row count
# before it is allowed to OVERWRITE a good projects.csv. Guards against a
# transient / blocked / partial scrape silently clobbering good data.
MIN_ROW_FRACTION = 0.5

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


# Each foundation's project list is one file under data/sources/funding/
# foundations/, named by the foundation's full (kebab-case) name. The scraper
# SLUG (the short host label used in host-by-repo.csv) maps to that filename here.
FOUNDATION_FILE = {
    "apache": "apache-software-foundation",
    "cncf": "cloud-native-computing-foundation",
    "eclipse": "eclipse-foundation",
    "fsf": "free-software-foundation",
    "gnome": "gnome",
    "gnu": "gnu-project",
    "lf": "linux-foundation",
    "xorg": "x-org-foundation",
    "numfocus": "numfocus",
    "openjs": "openjs-foundation",
    "psf": "python-software-foundation",
    "sfc": "software-freedom-conservancy",
}


def out_path(slug: str) -> Path:
    """Project-list path: data/sources/funding/foundations/<full-name>.csv."""
    name = FOUNDATION_FILE.get(slug, slug)
    p = DATA_DIR / "foundations" / f"{name}.csv"
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


def utc_now_iso() -> str:
    """Current UTC timestamp as an ISO-8601 string, seconds precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _existing_row_count(path: Path) -> int:
    """Number of data rows (excluding header) in an existing projects.csv."""
    if not path.exists():
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


def write_projects(slug: str, rows: list[dict], cols: list[str], *,
                   fetched_at: str | None = None, force: bool = False) -> tuple[Path, bool]:
    """Stamp `fetched_at`, apply the MIN-ROW GUARD, atomically write projects.csv.

    Returns `(path, written)`. When the new scrape produced too few rows to
    safely overwrite an existing good file, the old file is kept untouched and
    `written` is False (a loud warning is logged). On a real refresh the file
    is rewritten with a fresh `fetched_at`; within-TTL re-runs are gated out
    earlier by `is_output_fresh`, so the discovery layer stays idempotent.

    `force=True` (wired from each scraper's --force) bypasses the MIN-ROW GUARD:
    an operator explicitly refreshing may legitimately accept a much smaller
    project list (a foundation that genuinely shrank), so --force is the escape
    hatch when the guard would otherwise refuse forever.
    """
    path = out_path(slug)
    existing = _existing_row_count(path)
    # Refuse to clobber a good file with a 0-row / partial scrape — unless the
    # caller explicitly forced the refresh.
    if not force and existing and len(rows) < existing * MIN_ROW_FRACTION:
        console.print(
            f"  [bold yellow]GUARD[/bold yellow]: new scrape produced "
            f"[yellow]{len(rows):,}[/yellow] rows "
            f"(< {MIN_ROW_FRACTION:.0%} of existing {existing:,}) — keeping old "
            f"[cyan]{path}[/cyan], NOT overwriting. Re-run with [bold]--force[/bold] "
            f"if the shrink is real."
        )
        return path, False

    stamp = fetched_at or utc_now_iso()
    fieldnames = list(cols)
    if FETCHED_AT_COL not in fieldnames:
        fieldnames.append(FETCHED_AT_COL)
    stamped = [{**r, FETCHED_AT_COL: r.get(FETCHED_AT_COL) or stamp} for r in rows]
    atomic_write(path, stamped, fieldnames)
    return path, True


def parse_scraper_args(description: str = "") -> argparse.Namespace:
    """Standard CLI for every foundation scraper: a shared TTL + force escape.

    `--ttl 0` or `--force` both bypass the freshness gate and re-fetch.
    """
    p = argparse.ArgumentParser(description=description)
    p.add_argument(
        "--ttl", type=int, default=FUNDING_TTL_DAYS,
        help=f"Skip the fetch if projects.csv is younger than this many days "
             f"(default {FUNDING_TTL_DAYS}; 0 forces a refresh).",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Ignore the TTL and re-fetch unconditionally.",
    )
    return p.parse_args()


def is_output_fresh(slug: str, args: argparse.Namespace) -> bool:
    """True if projects.csv is within the TTL window — a re-run is a no-op.

    Prints a clear "fresh, skipping" line so a within-window re-run is obvious.
    """
    if getattr(args, "force", False):
        return False
    ttl = getattr(args, "ttl", FUNDING_TTL_DAYS)
    path = out_path(slug)
    if file_is_fresh(path, ttl):
        console.print(
            f"  [green]fresh[/green]: [cyan]{path}[/cyan] is within the "
            f"{ttl}-day TTL — skipping fetch (use --force or --ttl 0 to refresh)."
        )
        return True
    return False


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
