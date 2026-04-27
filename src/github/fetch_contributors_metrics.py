"""Contributor metrics — bus factor, HHI, contributor parsing, and CLI.

Uses GitHub's /repos/{repo}/stats/contributors endpoint. Contributors are keyed by
GitHub login (resolved server-side from commit email), so commits by the same user
under multiple verified emails collapse into one entry.

Usage:
    python -m src.github.fetch_contributors_metrics facebook/react          # all years from first activity
    python -m src.github.fetch_contributors_metrics facebook/react --years 2021 2025
    python -m src.github.fetch_contributors_metrics                         # batch: data/github/ab-repos.csv → contributors/*.csv
"""

import argparse
import asyncio
import datetime
import logging
import re
import time

from src.github.models import (
    Contributor, PerfStats, RunResult, DateRange,
    THRESHOLD, is_bot,
)
from src.github.github_client import fetch_contributor_stats
from src.github.display import _spinner, display_results, display_yearly_breakdown
from src.github.batch_runner import batch_update, _upsert_yearly_csv
from src.pipeline.repos import VALUE_FILE, load_ab_slugs


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


def _cumulative_loc(stats: list[dict], up_to: datetime.date) -> int:
    """Compute total repo LOC (additions - deletions) from all weeks up to a date."""
    total = 0
    cutoff = DateRange(until=up_to)
    for entry in stats:
        for w in entry.get("weeks", []):
            if _week_in_range(w, cutoff):
                total += w.get("a", 0) - w.get("d", 0)
    return total


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
        loc = _cumulative_loc(stats, datetime.date(y, 12, 31))
        year_results.append((str(y), RunResult(
            bus_factor=bf, contributors=sorted_c, hhi=hhi, perf=PerfStats(),
            first_week=fw, last_week=lw, total_loc=loc,
        )))

    contribs, fw, lw = _parse_api_stats(stats, date_range=total_dr)
    bf, sorted_c, hhi = _compute_bus_factor(contribs, threshold=threshold, base=base, include_bots=include_bots)
    loc = _cumulative_loc(stats, datetime.date(year_end, 12, 31))
    year_results.append((f"{year_start}-{year_end}", RunResult(
        bus_factor=bf, contributors=sorted_c, hhi=hhi, perf=PerfStats(),
        first_week=fw, last_week=lw, total_loc=loc,
    )))

    return year_results


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate bus factor for a GitHub repo")
    parser.add_argument("repo", nargs="?", help="GitHub repo URL or owner/repo slug")
    parser.add_argument("--input", default=VALUE_FILE,
                        help=f"value-data CSV (default: {VALUE_FILE} — "
                             f"loads A/B class repos with non-empty github_repo, skips archived)")
    parser.add_argument("--limit", type=int, help="Process N random repos from --input CSV")
    parser.add_argument("--base", choices=["commits", "locs"], default="commits",
                        help="Metric for bus factor (default: commits)")
    parser.add_argument("--threshold", type=float, default=THRESHOLD, help="Ownership threshold (default 0.5)")
    parser.add_argument("--years", type=int, nargs=2, metavar=("START", "END"),
                        help="Year range (default: first activity–now for single repo, 2021–2025 for batch)")
    parser.add_argument("--output",
                        help="Directory to upsert per-metric CSVs into (default: data/github/contributors/ in batch mode)")
    parser.add_argument("--include-bots", action="store_true", default=False,
                        help="Include bots as regular contributors in bus factor calculation")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.limit and args.repo:
        parser.error("--limit requires batch mode (no repo argument)")

    if args.verbose:
        logging.getLogger("src.github").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Batch mode (default when no repo argument given)
    if not args.repo:
        years = args.years or [2021, 2025]
        output = args.output or "data/github/contributors"
        repos = load_ab_slugs(value_file=args.input)
        asyncio.run(batch_update(
            repos, years[0], years[1], output,
            threshold=args.threshold, base=args.base,
            include_bots=args.include_bots,
            limit=args.limit,
        ))
        return

    repo = parse_repo(args.repo)
    t_start = time.monotonic()

    with _spinner("Fetching contributor stats from GitHub API..."):
        stats = fetch_contributor_stats(repo)
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
