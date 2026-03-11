"""Contributor metrics — bus factor, HHI, contributor parsing, git clone analysis, and CLI.

Usage:
    python -m src.github.contributors facebook/react          # all years from first activity
    python -m src.github.contributors facebook/react --years 2021 2025
    python -m src.github.contributors                         # batch: top-repos.csv → repo-contrib-metrics.csv
"""

import argparse
import asyncio
import datetime
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TaskProgressColumn, TimeElapsedColumn,
)

from src.github.models import (
    Contributor, PerfStats, RunResult, DateRange,
    THRESHOLD, KNOWN_BOTS, is_bot,
)
from src.github.api import fetch_contributor_stats, fetch_repo_info
from src.github.display import console, _spinner, display_results, display_yearly_breakdown
from src.github.batch import batch_update, _upsert_yearly_csv, _load_repos_from_csv

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".cache"


def _sanitize(name: str) -> str:
    """Sanitize a git author name: strip replacement chars and normalize whitespace."""
    name = name.replace("\ufffd", "").strip()
    return " ".join(name.split())


def parse_repo(url_or_slug: str) -> str:
    """Extract 'owner/repo' from a GitHub URL or slug."""
    url_or_slug = url_or_slug.strip().rstrip("/")
    m = re.match(r"(?:https?://github\.com/)?([^/]+/[^/]+?)(?:\.git)?$", url_or_slug)
    if not m:
        raise ValueError(f"Cannot parse repo from: {url_or_slug}")
    return m.group(1).lower()


def _week_in_range(week: dict, date_range: DateRange) -> bool:
    """Check if a weekly stats entry starts within the given date range."""
    if date_range.is_empty:
        return True
    ts = week.get("w", 0)
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).date()
    if date_range.since is not None and dt < date_range.since:
        return False
    if date_range.until is not None and dt > date_range.until:
        return False
    return True


def _compute_bus_factor(
    contributors: list[Contributor], threshold: float = THRESHOLD,
    base: str = "commits", include_bots: bool = False,
) -> tuple[int, list[Contributor], float]:
    """Core bus factor + HHI computation from a list of Contributors."""
    for c in contributors:
        c.is_bot = is_bot(c.login) and not include_bots

    active = [c for c in contributors if not c.is_bot]

    if base == "locs":
        total = sum(c.lines_changed for c in active)
        if total == 0:
            return 0, [], 0.0
        for c in active:
            c.pct = c.lines_changed / total
        sort_key = lambda c: c.lines_changed
    else:
        total = sum(c.commits for c in active)
        if total == 0:
            return 0, [], 0.0
        for c in active:
            c.pct = c.commits / total
        sort_key = lambda c: c.commits

    hhi = sum(c.pct ** 2 for c in active)

    cumulative = 0.0
    bus_factor = 0
    for c in sorted(active, key=sort_key, reverse=True):
        cumulative += c.pct
        bus_factor += 1
        if cumulative >= threshold:
            break

    all_sorted = sorted(contributors, key=sort_key, reverse=True)
    return bus_factor, all_sorted, hhi


def _parse_api_stats(
    stats: list[dict], date_range: DateRange | None = None,
) -> tuple[list[Contributor], datetime.date | None, datetime.date | None]:
    """Parse GitHub API stats into Contributor objects."""
    dr = date_range or DateRange()
    contributors = []
    all_timestamps: list[int] = []
    for entry in stats:
        author = entry.get("author") or {}
        login = author.get("login", "unknown").lower()
        weeks = [w for w in entry.get("weeks", []) if _week_in_range(w, dr)]
        for w in weeks:
            if w.get("c", 0) > 0 or w.get("a", 0) + w.get("d", 0) > 0:
                all_timestamps.append(w.get("w", 0))
        total_commits = sum(w.get("c", 0) for w in weeks)
        added = sum(w.get("a", 0) for w in weeks)
        deleted = sum(w.get("d", 0) for w in weeks)
        lines = added + deleted
        if lines == 0 and total_commits == 0:
            continue
        contributors.append(
            Contributor(login=login, lines_changed=lines, commits=total_commits,
                        lines_added=added, lines_deleted=deleted)
        )
    first_week = last_week = None
    if all_timestamps:
        first_week = datetime.datetime.fromtimestamp(min(all_timestamps), tz=datetime.timezone.utc).date()
        last_week_start = datetime.datetime.fromtimestamp(max(all_timestamps), tz=datetime.timezone.utc).date()
        last_week = last_week_start + datetime.timedelta(days=6)
    return contributors, first_week, last_week


def calculate_bus_factor(
    stats: list[dict], threshold: float = THRESHOLD, date_range: DateRange | None = None,
    base: str = "commits", include_bots: bool = False,
    year: int | None = None, year_end: int | None = None,
) -> tuple[int, list[Contributor], float]:
    """Calculate bus factor from GitHub contributor stats.

    Returns (bus_factor, sorted_contributors, hhi).
    HHI ranges from 1/N (perfectly equal) to 1.0 (monopoly).
    """
    if date_range is None and year is not None:
        date_range = DateRange.from_years(year, year_end)
    contributors, _, _ = _parse_api_stats(stats, date_range=date_range)
    return _compute_bus_factor(contributors, threshold=threshold, base=base, include_bots=include_bots)


def compute_yearly_breakdown(
    stats: list[dict], year_start: int, year_end: int,
    threshold: float = THRESHOLD, base: str = "commits",
    include_bots: bool = False,
) -> list[tuple[str, RunResult]]:
    """Compute metrics for each year + total. Returns list of (label, RunResult)."""
    years = list(range(year_start, year_end + 1))
    total_dr = DateRange.from_years(year_start, year_end)

    year_results: list[tuple[str, RunResult]] = []
    for y in years:
        dr = DateRange.from_years(y)
        contribs, fw, lw = _parse_api_stats(stats, date_range=dr)
        bf, sorted_c, hhi = _compute_bus_factor(contribs, threshold=threshold, base=base, include_bots=include_bots)
        year_results.append((str(y), RunResult(
            bus_factor=bf, contributors=sorted_c, hhi=hhi, perf=PerfStats(),
            first_week=fw, last_week=lw,
        )))

    contribs, fw, lw = _parse_api_stats(stats, date_range=total_dr)
    bf, sorted_c, hhi = _compute_bus_factor(contribs, threshold=threshold, base=base, include_bots=include_bots)
    year_results.append((f"{year_start}-{year_end}", RunResult(
        bus_factor=bf, contributors=sorted_c, hhi=hhi, perf=PerfStats(),
        first_week=fw, last_week=lw,
    )))

    return year_results


# --- Git clone analysis ---

_SHORTSTAT_RE = re.compile(
    r"\s*(\d+) files? changed(?:,\s*(\d+) insertions?\(\+\))?(?:,\s*(\d+) deletions?\(-\))?"
)


def _get_or_create_clone(
    repo: str, default_branch: str,
) -> tuple[str, bool]:
    """Get a cached bare clone or create a new one."""
    clone_url = f"https://github.com/{repo}.git"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = str(CACHE_DIR / (repo.replace("/", "-") + ".git"))

    if os.path.isdir(cache_path):
        log.debug("Updating cached clone at %s", cache_path)
        subprocess.run(
            ["git", "-C", cache_path, "fetch", "--quiet", "origin", default_branch],
            capture_output=True, timeout=120,
        )
        return cache_path, True

    log.debug("Creating cached clone at %s", cache_path)
    subprocess.run(
        ["git", "clone", "--bare", "--quiet", "--single-branch",
         "--filter=blob:none", f"--branch={default_branch}", clone_url, cache_path],
        check=True, capture_output=True, timeout=300,
    )
    return cache_path, False


def fetch_stats_via_git(
    repo: str, date_range: DateRange | None = None,
    api_stats: list[dict] | None = None,
    default_branch: str = "main",
    perf: PerfStats | None = None,
    size_kb: int = 0,
) -> list[Contributor]:
    """Clone repo bare and compute contributor stats via git log --shortstat."""
    dr = date_range or DateRange()

    est_sec = max(1, size_kb / 1024 / 10) if size_kb > 0 else 0
    size_label = f" (~{size_kb // 1024}MB, ~{est_sec:.0f}s)" if size_kb > 1024 else ""

    with _spinner(f"Cloning repo (bare){size_label}..."):
        t_clone = time.monotonic()
        clone_path, is_cached = _get_or_create_clone(repo, default_branch)
        if perf:
            perf.git_clone = time.monotonic() - t_clone

    cmd = ["git", "-C", clone_path, "log", "--format=COMMIT:%aN", "--shortstat",
           f"refs/heads/{default_branch}"]
    if dr.since:
        cmd += [f"--after={dr.since.isoformat()}"]
    if dr.until:
        day_after = dr.until + datetime.timedelta(days=1)
        cmd += [f"--before={day_after.isoformat()}"]
    log.debug("Running: %s", " ".join(cmd))

    t_parse = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace")

    author_added: dict[str, int] = defaultdict(int)
    author_deleted: dict[str, int] = defaultdict(int)
    author_commits: dict[str, int] = defaultdict(int)
    current_author = None
    commit_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=20),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        total_estimate = sum(e.get("total", 0) for e in (api_stats or []))
        parse_task = progress.add_task(
            "Analyzing commits...",
            total=total_estimate if total_estimate > 0 else None,
        )

        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("COMMIT:"):
                current_author = _sanitize(line[7:]).lower()
                author_commits[current_author] += 1
                commit_count += 1
                progress.update(parse_task, completed=commit_count,
                                description=f"Analyzing commits ({commit_count:,})...")
            else:
                m = _SHORTSTAT_RE.match(line)
                if m and current_author:
                    added = int(m.group(2)) if m.group(2) else 0
                    deleted = int(m.group(3)) if m.group(3) else 0
                    author_added[current_author] += added
                    author_deleted[current_author] += deleted

    proc.wait()
    if perf:
        perf.git_parse = time.monotonic() - t_parse
        perf.commits_analyzed = commit_count

    contributors = []
    for author in author_commits:
        added = author_added.get(author, 0)
        deleted = author_deleted.get(author, 0)
        contributors.append(Contributor(
            login=author,
            lines_changed=added + deleted,
            commits=author_commits[author],
            lines_added=added,
            lines_deleted=deleted,
        ))
    log.debug("Git clone: found %d contributors from %d commits",
              len(contributors), commit_count)
    return contributors


def run(
    repo_url: str, token: str | None = None, date_range: DateRange | None = None,
    threshold: float = THRESHOLD, base: str = "commits",
    source: str = "github", include_bots: bool = False,
) -> RunResult:
    """Main entry point. source='github' uses API; source='git' clones the repo."""
    repo = parse_repo(repo_url)
    log.debug("Analyzing %s (base=%s, source=%s)", repo, base, source)
    perf = PerfStats(source=source)
    t_start = time.monotonic()

    first_week = last_week = None

    if source == "git":
        t_api_start = time.monotonic()
        stats: list[dict] = []
        try:
            with _spinner("Fetching contributor stats from GitHub API..."):
                stats = fetch_contributor_stats(repo, token=token)
        except RuntimeError:
            pass
        perf.api_fetch = time.monotonic() - t_api_start

        with _spinner("Fetching repo info..."):
            default_branch, size_kb = fetch_repo_info(repo, token=token)

        contributors = fetch_stats_via_git(
            repo, date_range=date_range, api_stats=stats,
            default_branch=default_branch, perf=perf, size_kb=size_kb,
        )
    else:
        t_api_start = time.monotonic()
        with _spinner("Fetching contributor stats from GitHub API..."):
            stats = fetch_contributor_stats(repo, token=token)
        perf.api_fetch = time.monotonic() - t_api_start

        dr = date_range or DateRange()
        contributors, first_week, last_week = _parse_api_stats(stats, date_range=dr)
        perf.commits_analyzed = sum(c.commits for c in contributors)

    bf, contribs, hhi = _compute_bus_factor(contributors, threshold=threshold, base=base, include_bots=include_bots)

    perf.contributors_found = len(contribs)
    perf.total = time.monotonic() - t_start
    return RunResult(
        bus_factor=bf, contributors=contribs, hhi=hhi, perf=perf,
        first_week=first_week, last_week=last_week,
    )


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate bus factor for a GitHub repo")
    parser.add_argument("repo", nargs="?", help="GitHub repo URL or owner/repo slug")
    parser.add_argument("--input", default="data/github/top-repos.csv",
                        help="CSV file with repos to batch update (expects 'repo' column, default: data/github/top-repos.csv)")
    parser.add_argument("--top", type=int, help="Only process first N repos from --input CSV")
    parser.add_argument("--base", choices=["commits", "locs"], default="commits",
                        help="Metric for bus factor (default: commits)")
    parser.add_argument("--token", help="GitHub personal access token")
    parser.add_argument("--threshold", type=float, default=THRESHOLD, help="Ownership threshold (default 0.5)")
    parser.add_argument("--years", type=int, nargs=2, metavar=("START", "END"),
                        help="Year range (default: first activity–now for single repo, 2021–2025 for batch)")
    parser.add_argument("--output",
                        help="CSV file to upsert yearly metrics into (default: repo-contrib-metrics.csv in batch mode)")
    parser.add_argument("--include-bots", action="store_true", default=False,
                        help="Include bots as regular contributors in bus factor calculation")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.top and args.repo:
        parser.error("--top requires batch mode (no repo argument)")

    if args.verbose:
        logging.getLogger("src.github").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    load_dotenv()
    token = args.token or os.environ.get("GITHUB_TOKEN")

    # Batch mode (default when no repo argument given)
    if not args.repo:
        years = args.years or [2021, 2025]
        output = args.output or "data/github/repo-contrib-metrics.csv"
        repos = _load_repos_from_csv(args.input, top=args.top)
        asyncio.run(batch_update(
            repos, years[0], years[1], output,
            threshold=args.threshold, base=args.base,
            include_bots=args.include_bots, token=token,
        ))
        return

    repo = parse_repo(args.repo)
    t_start = time.monotonic()

    with _spinner("Fetching contributor stats from GitHub API..."):
        stats = fetch_contributor_stats(repo, token=token)
    api_time = time.monotonic() - t_start

    # Determine year range: explicit --years, or auto-detect from first activity
    if args.years:
        year_start, year_end = args.years
    else:
        _, first_week, _ = _parse_api_stats(stats)
        last_complete_year = datetime.datetime.now().year - 1
        year_start = first_week.year if first_week else last_complete_year
        year_end = last_complete_year

    date_range = DateRange.from_years(year_start, year_end)

    year_results = compute_yearly_breakdown(stats, year_start, year_end,
                                            threshold=args.threshold, base=args.base,
                                            include_bots=args.include_bots)
    display_yearly_breakdown(repo, year_results, base=args.base)

    if args.output:
        _upsert_yearly_csv(args.output, repo, year_results)

    _, total_result = year_results[-1]
    total_result.perf = PerfStats(
        source="api", api_fetch=api_time, total=time.monotonic() - t_start,
        commits_analyzed=sum(c.commits for c in total_result.contributors if not c.is_bot),
        contributors_found=len(total_result.contributors),
    )
    display_results(repo, total_result, date_range=date_range, base=args.base, skip_summary=True)


if __name__ == "__main__":
    main()
