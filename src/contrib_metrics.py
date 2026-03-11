"""Contributor Metrics — computes bus factor, HHI, and other contributor metrics for any GitHub repo."""

import argparse
import asyncio
import csv as csv_mod
import datetime
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.progress import Progress, ProgressColumn, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn, Task as RichTask
from rich.table import Table

from rich.text import Text


class _ETAColumn(ProgressColumn):
    """Shows ETA as absolute wall-clock time (e.g. 'ETA 14:32:05')."""

    def render(self, task: RichTask) -> Text:
        remaining = task.time_remaining
        if remaining is None or remaining == float("inf"):
            return Text("", style="dim")
        eta = datetime.datetime.now() + datetime.timedelta(seconds=remaining)
        return Text(f"ETA {eta:%H:%M:%S}", style="dim")


logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

console = Console()

GITHUB_API = "https://api.github.com"
THRESHOLD = 0.5  # 50% of total work
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

# Reuse a single requests session for connection pooling + keep-alive
_session = requests.Session()
_session.headers.update({"Accept": "application/vnd.github+json"})

@contextmanager
def _spinner(message: str):
    """Context manager for a transient spinner progress bar."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(message, total=None)
        yield progress


def _fmt_date(d: datetime.date | None, default: str = "–") -> str:
    """Format a date for display."""
    return d.isoformat() if d else default


def concentration_risk_class(bus_factor: int, hhi: int) -> str:
    """Classify repo concentration risk as A/B/C/D.

    A (critical):  BF=1  and HHI >= 8000 — single maintainer, extreme concentration
    B (high risk): BF<=2 and HHI >= 5000 — tiny core, high concentration
    C (moderate):  BF<=4 and HHI >= 2500 — small team, moderate concentration
    D (healthy):   everything else        — distributed enough
    """
    if bus_factor == 1 and hhi >= 8000:
        return "A"
    if bus_factor <= 2 and hhi >= 5000:
        return "B"
    if bus_factor <= 4 and hhi >= 2500:
        return "C"
    return "D"


def _fmt_contribs_label(contributors: list["Contributor"]) -> str:
    """Format contributor count as 'N' or 'B + N' with dimmed bot count."""
    humans = [c for c in contributors if not c.is_bot]
    bots = [c for c in contributors if c.is_bot]
    label = ""
    if bots:
        label += f"[bright_black]{len(bots)} +[/bright_black]"
    label += f"{len(humans):,}"
    return label


# Known bot accounts — excluded from bus factor calculation, shown dimmed in output
KNOWN_BOTS: set[str] = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "cloudflare-workers-and-pages",
    "web-flow",
    "greenkeeper[bot]",
    "snyk-bot",
    "codecov-commenter",
    "semantic-release-bot",
    "allcontributors[bot]",
    "imgbot[bot]",
    "pyup-bot",
    "mend-bolt-for-github[bot]",
    "copilot",
    "claude",
    "pre-commit-ci[bot]",
    "netlify[bot]",
    "vercel[bot]",
    "mergify[bot]",
    "renovate-bot",
    "depfu[bot]",
}


def _is_bot(login: str) -> bool:
    """Check if a login belongs to a known bot."""
    return login in KNOWN_BOTS or login.endswith("[bot]")


@dataclass
class Contributor:
    login: str
    lines_changed: int
    commits: int
    lines_added: int = 0
    lines_deleted: int = 0
    pct: float = 0.0
    name: str = ""
    is_bot: bool = False

    @property
    def loc(self) -> int:
        """Net lines of code (additions - deletions)."""
        return self.lines_added - self.lines_deleted


@dataclass
class PerfStats:
    """Timing stats for each phase."""
    total: float = 0.0
    api_fetch: float = 0.0
    git_clone: float = 0.0
    git_parse: float = 0.0
    source: str = "api"  # "api" or "git"
    commits_analyzed: int = 0
    contributors_found: int = 0


@dataclass
class RunResult:
    bus_factor: int
    contributors: list[Contributor]
    hhi: float
    perf: PerfStats
    first_week: datetime.date | None = None
    last_week: datetime.date | None = None


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


def _init_session(token: str | None = None) -> None:
    """Set auth token on the shared session."""
    if token:
        _session.headers["Authorization"] = f"Bearer {token}"


def fetch_repo_info(repo: str, token: str | None = None) -> tuple[str, int]:
    """Fetch default branch and repo size (KB) from GitHub API."""
    _init_session(token)
    url = f"{GITHUB_API}/repos/{repo}"
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    branch = data.get("default_branch", "main")
    size_kb = data.get("size", 0)
    log.debug("Default branch for %s: %s, size: %d KB", repo, branch, size_kb)
    return branch, size_kb


def fetch_contributor_stats(
    repo: str, token: str | None = None, retries: int = 6
) -> list[dict]:
    """Fetch contributor stats from GitHub API. Handles 202/204 (computing) responses.

    Returns [] for repos with no contributor stats (e.g. no commits with emails).
    """
    _init_session(token)
    url = f"{GITHUB_API}/repos/{repo}/stats/contributors"

    for attempt in range(retries):
        log.debug("Fetching stats for %s (attempt %d)", repo, attempt + 1)
        resp = _session.get(url, timeout=30)

        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data
            # 200 with empty body = stats computed but genuinely empty
            log.debug("%s: no contributor stats available", repo)
            return []

        if resp.status_code in (202, 204):
            wait = min(2.0 + attempt * 2, 10.0)
            log.debug("GitHub is computing stats (%d), retrying in %.1fs...", resp.status_code, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            raise RuntimeError(
                f"Rate limited (remaining: {remaining}). Set GITHUB_TOKEN."
            )

        if resp.status_code == 404:
            raise RuntimeError(f"Repo not found: {repo}")

        resp.raise_for_status()

    log.warning("%s: no stats after %d retries, treating as empty", repo, retries)
    return []


@dataclass
class DateRange:
    """Inclusive date range for filtering."""
    since: datetime.date | None = None
    until: datetime.date | None = None

    @staticmethod
    def from_years(year: int | None, year_end: int | None = None) -> "DateRange":
        if year is None:
            return DateRange()
        since = datetime.date(year, 1, 1)
        end_year = year_end if year_end is not None else year
        until = datetime.date(end_year, 12, 31)
        return DateRange(since=since, until=until)

    @staticmethod
    def from_dates(since: str | None, until: str | None) -> "DateRange":
        s = datetime.date.fromisoformat(since) if since else None
        u = datetime.date.fromisoformat(until) if until else None
        return DateRange(since=s, until=u)

    @property
    def is_empty(self) -> bool:
        return self.since is None and self.until is None

    @property
    def label(self) -> str:
        if self.is_empty:
            return ""
        if self.since and self.until:
            # If both are exact year boundaries, show as year range
            if self.since.month == 1 and self.since.day == 1 and self.until.month == 12 and self.until.day == 31:
                if self.since.year == self.until.year:
                    return str(self.since.year)
                return f"{self.since.year}–{self.until.year}"
            return f"{self.since} to {self.until}"
        if self.since:
            return f"since {self.since}"
        return f"until {self.until}"


def _week_in_range(week: dict, date_range: DateRange) -> bool:
    """Check if a weekly stats entry starts within the given date range.

    GitHub weeks are 7-day spans starting at the timestamp (always Sundays).
    A week is included if its start date falls within [since, until].
    The last week may end after `until` — that's expected.
    """
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
    """Core bus factor + HHI computation from a list of Contributors.

    base: "commits" or "locs" — which metric to use for percentages.
    Bots are excluded from bus factor/HHI calculation unless include_bots=True.
    Returns (bus_factor, sorted_contributors, hhi).
    """
    # Tag bots
    for c in contributors:
        c.is_bot = _is_bot(c.login) and not include_bots

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

    # Sort all contributors together by the same metric
    all_sorted = sorted(contributors, key=sort_key, reverse=True)
    return bus_factor, all_sorted, hhi


def _parse_api_stats(
    stats: list[dict], date_range: DateRange | None = None
) -> tuple[list[Contributor], datetime.date | None, datetime.date | None]:
    """Parse GitHub API stats into Contributor objects.

    Returns (contributors, first_week_date, last_week_date).
    """
    dr = date_range or DateRange()
    contributors = []
    all_timestamps: list[int] = []
    for entry in stats:
        author = entry.get("author") or {}
        login = author.get("login", "unknown").lower()
        weeks = [w for w in entry.get("weeks", []) if _week_in_range(w, dr)]
        # Collect timestamps of weeks with activity
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


def _get_or_create_clone(
    repo: str, default_branch: str,
) -> tuple[str, bool]:
    """Get a cached bare clone or create a new one.

    Returns (clone_path, was_cached).
    Cached clones are stored in ~/.cache/busfactor/{owner}-{repo}.git
    and updated with git fetch on reuse.
    """
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


_SHORTSTAT_RE = re.compile(
    r"\s*(\d+) files? changed(?:,\s*(\d+) insertions?\(\+\))?(?:,\s*(\d+) deletions?\(-\))?"
)


def fetch_stats_via_git(
    repo: str, date_range: DateRange | None = None,
    api_stats: list[dict] | None = None,
    default_branch: str = "main",
    perf: PerfStats | None = None,
    size_kb: int = 0,
) -> list[Contributor]:
    """Clone repo bare and compute contributor stats via git log --shortstat."""
    dr = date_range or DateRange()

    # Estimate clone time: ~10 MB/s for git clone over HTTPS
    est_sec = max(1, size_kb / 1024 / 10) if size_kb > 0 else 0
    size_label = f" (~{size_kb // 1024}MB, ~{est_sec:.0f}s)" if size_kb > 1024 else ""

    with _spinner(f"Cloning repo (bare){size_label}..."):
        t_clone = time.monotonic()
        clone_path, is_cached = _get_or_create_clone(repo, default_branch)
        if perf:
            perf.git_clone = time.monotonic() - t_clone

    # Build git log command with --shortstat (much less output than --numstat)
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



def calculate_bus_factor(
    stats: list[dict], threshold: float = THRESHOLD, date_range: DateRange | None = None,
    base: str = "commits", include_bots: bool = False,
    # Legacy year args — prefer date_range
    year: int | None = None, year_end: int | None = None,
) -> tuple[int, list[Contributor], float]:
    """Calculate bus factor from GitHub contributor stats.

    Returns (bus_factor, sorted_contributors, hhi).
    HHI (Herfindahl-Hirschman Index) ranges from 1/N (perfectly equal) to 1.0 (monopoly).
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


def display_yearly_breakdown(
    repo: str, year_results: list[tuple[str, RunResult]],
    base: str = "commits",
) -> None:
    """Show a table with per-year rows for key metrics."""
    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Year", style="dim")
    table.add_column("BF", justify="right")
    table.add_column("HHI", justify="right")
    table.add_column("Contribs", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("First", justify="right")
    table.add_column("Last", justify="right")

    total_label = year_results[-1][0]
    year_labels = [label for label, _ in year_results[:-1]]

    for label, r in year_results:
        humans = [c for c in r.contributors if not c.is_bot]
        total_commits = sum(c.commits for c in humans)
        is_total = label == total_label

        table.add_row(
            f"[bold]{label}[/bold]" if is_total else label,
            f"[yellow bold]{r.bus_factor}[/yellow bold]" if r.bus_factor > 0 else "[dim]–[/dim]",
            f"{round(r.hhi * 10000):,}" if r.hhi > 0 else "[dim]–[/dim]",
            _fmt_contribs_label(r.contributors) if humans else "[dim]–[/dim]",
            f"{total_commits:,}" if total_commits else "[dim]–[/dim]",
            _fmt_date(r.first_week),
            _fmt_date(r.last_week),
            end_section=(label == year_labels[-1]),
        )

    console.print()
    range_label = f" ([magenta]{total_label}[/magenta])"
    base_label = f" [dim]by {base}[/dim]"
    console.print(f"[bold]Bus Factor for [cyan]{repo}[/cyan]{range_label}{base_label}[/bold]")
    console.print()
    console.print(table)


def display_results(repo: str, result: RunResult, date_range: DateRange | None = None,
                    base: str = "commits", skip_summary: bool = False) -> None:
    """Print a rich table of results."""
    bus_factor = result.bus_factor
    contributors = result.contributors
    hhi = result.hhi
    perf = result.perf

    if not skip_summary:
        console.print()
        dr = date_range or DateRange()
        range_label = f" ([magenta]{dr.label}[/magenta])" if dr.label else ""
        base_label = f" [dim]by {base}[/dim]"
        console.print(f"[bold]Bus Factor for [cyan]{repo}[/cyan]{range_label}{base_label}: [yellow]{bus_factor}[/yellow][/bold]")
        console.print()

        # Summary table
        human_contribs = [c for c in contributors if not c.is_bot]
        total_commits = sum(c.commits for c in human_contribs)
        summary = Table(show_header=False, padding=(0, 1), box=None)
        summary.add_column(style="dim")
        summary.add_column()
        summary.add_row("Bus Factor", f"[yellow bold]{bus_factor}[/yellow bold]")
        summary.add_row("HHI", f"{round(hhi * 10000):,}")
        summary.add_row("Contributors", _fmt_contribs_label(contributors))
        summary.add_row("Commits", f"{total_commits:,}")
        if result.first_week and result.last_week:
            summary.add_row("First Date", str(result.first_week))
            summary.add_row("Last Date", str(result.last_week))
        if base == "locs":
            total_changed = sum(c.lines_changed for c in contributors)
            total_loc = sum(c.loc for c in contributors)
            summary.add_row("Lines Changed", f"{total_changed:,}")
            summary.add_row("Net LOC", f"{total_loc:+,}")
        console.print(summary)
    console.print()

    has_locs = any(c.lines_changed > 0 for c in contributors)
    loc_header = "[bold underline]LOC[/bold underline]" if base == "locs" else "LOC"
    changed_header = "[bold underline]Changed[/bold underline]" if base == "locs" else "Changed"
    commits_header = "[bold underline]Commits[/bold underline]" if base == "commits" else "Commits"

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("#", style="dim", width=4)
    table.add_column("Contributor", min_width=16)
    table.add_column(loc_header, justify="right")
    table.add_column(changed_header, justify="right")
    table.add_column(commits_header, justify="right")
    table.add_column("%", justify="right")
    table.add_column("Cumul%", justify="right")

    cumulative = 0.0
    threshold_crossed = False
    show_count = min(20, len(contributors))
    human_idx = 0

    for c in contributors[:show_count]:
        if not c.is_bot:
            cumulative += c.pct
            human_idx += 1

        display_name = rich_escape(c.login)
        if c.is_bot:
            display_name = f"[bright_black]{display_name}[/bright_black]"
            num_str = ""
        else:
            num_str = str(human_idx)

        loc_str = f"{c.loc:+,}" if has_locs and (c.lines_added or c.lines_deleted) else ""
        changed_str = f"{c.lines_changed:,}" if has_locs and c.lines_changed else ""
        if c.is_bot:
            row = [num_str, display_name,
                   f"[bright_black]{loc_str}[/bright_black]",
                   f"[bright_black]{changed_str}[/bright_black]",
                   f"[bright_black]{c.commits:,}[/bright_black]", "", ""]
            table.add_row(*row)
        else:
            row = [num_str, display_name, loc_str, changed_str,
                   f"{c.commits:,}", f"{c.pct:.1%}", f"{cumulative:.1%}"]
            table.add_row(*row, style="yellow" if not threshold_crossed else "")

        if not c.is_bot and not threshold_crossed and cumulative >= THRESHOLD:
            threshold_crossed = True

    if len(contributors) > show_count:
        rest = contributors[show_count:]
        rest_humans = [c for c in rest if not c.is_bot]
        rest_commits = sum(c.commits for c in rest_humans)
        rest_pct = sum(c.pct for c in rest_humans)
        rest_loc = sum(c.loc for c in rest_humans)
        rest_lines = sum(c.lines_changed for c in rest_humans)
        has_rest_loc = has_locs and any(c.lines_added or c.lines_deleted for c in rest_humans)
        rest_bots = len(rest) - len(rest_humans)
        more_label = f"{len(rest_humans)} more"
        if rest_bots:
            more_label += f" + {rest_bots} bots"
        row = ["", f"[dim]... {more_label}[/dim]",
               f"[dim]{rest_loc:+,}[/dim]" if has_rest_loc else "",
               f"[dim]{rest_lines:,}[/dim]" if has_locs and rest_lines else "",
               f"[dim]{rest_commits:,}[/dim]", f"[dim]{rest_pct:.1%}[/dim]", "[dim]100.0%[/dim]"]
        table.add_row(*row)

    console.print(table)

    # Perf stats
    if perf:
        console.print()
        parts = [f"[dim]{perf.total:.1f}s total[/dim]"]
        if perf.source == "git":
            parts.append(f"[dim]clone {perf.git_clone:.1f}s[/dim]")
            parts.append(f"[dim]parse {perf.git_parse:.1f}s[/dim]")
        parts.append(f"[dim]api {perf.api_fetch:.1f}s[/dim]")
        console.print(" · ".join(parts))
    console.print()



def run(repo_url: str, token: str | None = None, date_range: DateRange | None = None, threshold: float = THRESHOLD, base: str = "commits", source: str = "github", include_bots: bool = False) -> RunResult:
    """Main entry point. source='github' uses API; source='git' clones the repo."""
    repo = parse_repo(repo_url)
    log.debug("Analyzing %s (base=%s, source=%s)", repo, base, source)
    perf = PerfStats(source=source)
    t_start = time.monotonic()

    first_week = last_week = None

    if source == "git":
        # Fetch API stats for progress estimates (optional)
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
        # GitHub API source
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


def _build_metrics_extractors() -> dict[str, object]:
    """Build metrics extractors for CSV output."""
    def _human_commits(r: RunResult) -> int:
        return sum(c.commits for c in r.contributors if not c.is_bot)

    def _risk_class(r: RunResult) -> str:
        if _human_commits(r) <= 0:
            return ""
        hhi_val = round(r.hhi * 10000)
        return concentration_risk_class(r.bus_factor, hhi_val)

    return {
        "bus_factor": lambda r: str(r.bus_factor) if _human_commits(r) > 0 else "",
        "hhi": lambda r: str(round(r.hhi * 10000)) if _human_commits(r) > 0 else "",
        "concentration_risk_class": _risk_class,
        "contributors": lambda r: str(len([c for c in r.contributors if not c.is_bot])),
        "bots": lambda r: str(len([c for c in r.contributors if c.is_bot])),
        "commits": lambda r: str(_human_commits(r)),
        "first_date": lambda r: _fmt_date(r.first_week, ""),
        "last_date": lambda r: _fmt_date(r.last_week, ""),
    }


def _upsert_yearly_csv(
    filepath: str, repo: str, year_results: list[tuple[str, RunResult]],
    quiet: bool = False,
) -> None:
    """Upsert yearly metrics for a repo into a CSV file.

    Columns: github_repo, metric, {year1}, {year2}, ..., {year1}-{year2}
    Metrics: bus_factor, hhi, contributors, bots, commits, first_date, last_date
    """
    col_labels = [label for label, _ in year_results]

    # Build rows for this repo
    new_rows: dict[str, dict[str, str]] = {}
    metrics = _build_metrics_extractors()

    for metric_name, extractor in metrics.items():
        row_data: dict[str, str] = {"github_repo": repo, "metric": metric_name}
        for label, r in year_results:
            row_data[label] = extractor(r)
        new_rows[metric_name] = row_data

    fieldnames = ["github_repo", "metric"] + col_labels

    # Read existing data
    existing_rows: list[dict[str, str]] = []
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            reader = csv_mod.DictReader(f)
            if reader.fieldnames:
                # Merge fieldnames (existing file may have different years)
                for fn in reader.fieldnames:
                    if fn not in fieldnames:
                        fieldnames.append(fn)
            existing_rows = list(reader)

    # Upsert: replace rows for this repo, keep others
    output_rows: list[dict[str, str]] = []
    seen_repo = False
    for row in existing_rows:
        if row.get("github_repo") == repo:
            seen_repo = True
            metric = row.get("metric", "")
            if metric in new_rows:
                output_rows.append(new_rows.pop(metric))
            # else: old metric no longer produced, skip it
        else:
            output_rows.append(row)

    # Append any new metrics not yet in the file
    for metric_name in metrics:
        if metric_name in new_rows:
            output_rows.append(new_rows[metric_name])

    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)

    if not quiet:
        action = "Updated" if seen_repo else "Added"
        console.print(f"[dim]{action} {repo} in {filepath}[/dim]")


def _upsert_yearly_csv_batch(
    filepath: str, batch: list[tuple[str, list[tuple[str, "RunResult"]]]],
) -> None:
    """Upsert yearly metrics for multiple repos in one CSV read/write cycle."""
    if not batch:
        return

    # Build new rows for all repos in the batch
    all_col_labels: list[str] = []
    all_new: dict[str, dict[str, dict[str, str]]] = {}  # repo -> metric -> row
    metrics = _build_metrics_extractors()

    for repo, year_results in batch:
        col_labels = [label for label, _ in year_results]
        for lbl in col_labels:
            if lbl not in all_col_labels:
                all_col_labels.append(lbl)
        repo_rows: dict[str, dict[str, str]] = {}
        for metric_name, extractor in metrics.items():
            row_data: dict[str, str] = {"github_repo": repo, "metric": metric_name}
            for label, r in year_results:
                row_data[label] = extractor(r)
            repo_rows[metric_name] = row_data
        all_new[repo] = repo_rows

    fieldnames = ["github_repo", "metric"] + all_col_labels

    # Read existing data
    existing_rows: list[dict[str, str]] = []
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            reader = csv_mod.DictReader(f)
            if reader.fieldnames:
                for fn in reader.fieldnames:
                    if fn not in fieldnames:
                        fieldnames.append(fn)
            existing_rows = list(reader)

    # Upsert: replace rows for batch repos, keep others
    output_rows: list[dict[str, str]] = []
    seen_repos: set[str] = set()
    for row in existing_rows:
        repo = row.get("github_repo", "")
        if repo in all_new:
            if repo not in seen_repos:
                seen_repos.add(repo)
                # Insert all metric rows for this repo
                for metric_name in metrics:
                    if metric_name in all_new[repo]:
                        output_rows.append(all_new[repo][metric_name])
        else:
            output_rows.append(row)

    # Append repos not previously in the file
    for repo, repo_rows in all_new.items():
        if repo not in seen_repos:
            for metric_name in metrics:
                if metric_name in repo_rows:
                    output_rows.append(repo_rows[metric_name])

    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)


# --- Async batch mode ---

class _AsyncRateLimiter:
    """Rate limiter for async GitHub API calls, driven by X-RateLimit-* headers."""

    def __init__(self) -> None:
        self._remaining: int | None = None
        self._reset_at: float = 0.0
        self._lock = asyncio.Lock()
        self._request_count = 0

    async def get(self, session: aiohttp.ClientSession, url: str, **kwargs) -> aiohttp.ClientResponse:
        async with self._lock:
            if self._remaining is not None and self._remaining <= 1:
                wait = self._reset_at - time.time() + 0.5
                if wait > 0:
                    log.info("Rate limit: sleeping %.1fs", wait)
                    await asyncio.sleep(wait)
            resp = await session.get(url, **kwargs)
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            if remaining is not None:
                self._remaining = int(remaining)
            if reset is not None:
                self._reset_at = float(reset)
            self._request_count += 1
            return resp

    @property
    def requests_made(self) -> int:
        return self._request_count


class _Deferred(Exception):
    """Raised when GitHub returns 202/204 — repo needs retry later."""


class _NoStats(Exception):
    """Raised when GitHub confirms no contributor stats exist (200 with empty body)."""


async def _fetch_stats_once(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter,
    repo: str,
) -> list[dict]:
    """Single attempt to fetch stats. Raises _Deferred on 202/204, _NoStats on empty 200."""
    url = f"{GITHUB_API}/repos/{repo}/stats/contributors"
    try:
        resp = await limiter.get(session, url)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise _Deferred() from e
    async with resp:
        if resp.status == 200:
            data = await resp.json()
            if data:
                return data
            # 200 with empty body = stats computed but genuinely empty
            raise _NoStats()
        if resp.status in (202, 204):
            raise _Deferred()
        if resp.status == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            raise RuntimeError(f"Rate limited ({repo}, remaining: {remaining})")
        if resp.status == 404:
            raise RuntimeError(f"Repo not found: {repo}")
        raise RuntimeError(f"{repo}: HTTP {resp.status}")


def _read_existing_periods(filepath: str) -> dict[str, set[str]]:
    """Read CSV and return {repo: set of period labels already collected}.

    A period is "collected" if the repo has a bus_factor row with that column
    in the header — even if the cell is empty (no activity that year).
    """
    if not os.path.exists(filepath):
        return {}
    result: dict[str, set[str]] = {}
    with open(filepath, encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        period_cols = {fn for fn in (reader.fieldnames or [])
                       if fn not in ("github_repo", "metric")}
        for row in reader:
            repo = row.get("github_repo", "")
            if row.get("metric") != "bus_factor":
                continue
            result[repo] = period_cols
    return result


async def _process_repo(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter,
    repo: str, year_start: int, year_end: int,
    threshold: float, base: str, include_bots: bool,
) -> list[tuple[str, RunResult]] | None:
    """Fetch stats and compute yearly breakdown for one repo. Returns None on error."""
    try:
        stats = await _fetch_stats_async(session, limiter, repo)
    except RuntimeError as e:
        console.print(f"  [red]Error[/red] {repo}: {e}")
        return None
    return compute_yearly_breakdown(stats, year_start, year_end,
                                     threshold=threshold, base=base,
                                     include_bots=include_bots)


async def batch_update(
    repos: list[str], year_start: int, year_end: int, output: str,
    threshold: float = THRESHOLD, base: str = "commits",
    include_bots: bool = False, token: str | None = None,
) -> None:
    """Fetch and update metrics for multiple repos in parallel."""
    t_start = time.monotonic()

    # Determine which repos need updating
    existing = _read_existing_periods(output)
    needed_labels = {str(y) for y in range(year_start, year_end + 1)}
    needed_labels.add(f"{year_start}-{year_end}")

    to_fetch: list[str] = []
    skipped = 0
    for repo in repos:
        filled = existing.get(repo, set())
        missing = needed_labels - filled
        if not missing:
            skipped += 1
        else:
            to_fetch.append(repo)

    console.print(f"[bold]Batch update[/bold]: {len(repos)} repos, "
                  f"{len(to_fetch)} to fetch, {skipped} skipped")
    if not to_fetch:
        console.print("[dim]Nothing to update.[/dim]")
        return

    headers = {
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(10)
    total_label = f"{year_start}-{year_end}"
    MAX_ROUNDS = 6
    ROUND_DELAY = 10  # seconds between retry rounds

    @dataclass
    class _RepoResult:
        repo: str
        year_results: list[tuple[str, RunResult]] | None
        elapsed: float
        error: str = ""
        rounds: int = 1  # how many rounds it took

    _NO_STATS = "__no_stats__"

    async def _try_one(repo: str) -> tuple[str, list[dict] | None, str, float]:
        """Try fetching stats once. Returns (repo, data|None, error, api_time).

        error=_NO_STATS means stats are confirmed empty (don't retry).
        error="" with data=None means deferred (retry next round).
        """
        async with sem:
            t = time.monotonic()
            try:
                data = await _fetch_stats_once(session, limiter, repo)
                return (repo, data, "", time.monotonic() - t)
            except _Deferred:
                return (repo, None, "", time.monotonic() - t)
            except _NoStats:
                return (repo, None, _NO_STATS, time.monotonic() - t)
            except RuntimeError as e:
                return (repo, None, str(e), time.monotonic() - t)

    results: list[_RepoResult] = []
    repo_api_time: dict[str, float] = defaultdict(float)  # cumulative API time per repo

    async with aiohttp.ClientSession(headers=headers) as session:
        max_name = max(len(r) for r in to_fetch) if to_fetch else 20
        name_width = min(max_name, 30)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}", justify="left"),
            BarColumn(bar_width=20),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            _ETAColumn(),
            console=console,
        ) as progress:
            pad = " " * name_width
            task = progress.add_task(f"{pad} ...", total=len(to_fetch))

            FLUSH_EVERY = 50
            pending_flush: list[tuple[str, list[tuple[str, RunResult]]]] = []
            pending_repos = list(to_fetch)

            for round_num in range(MAX_ROUNDS):
                if not pending_repos:
                    break

                if round_num > 0:
                    progress.update(task,
                                    description=f"{'retry round ' + str(round_num + 1):>{name_width}} "
                                    f"({len(results)}/{len(to_fetch)})")
                    await asyncio.sleep(ROUND_DELAY)

                deferred: list[str] = []
                coros = [_try_one(repo) for repo in pending_repos]
                for coro in asyncio.as_completed(coros):
                    repo, data, error, api_time = await coro
                    repo_api_time[repo] += api_time

                    if error and error != _NO_STATS:
                        # Hard error (404, rate limit, etc.)
                        results.append(_RepoResult(repo=repo, year_results=None,
                                                   elapsed=repo_api_time[repo], error=error,
                                                   rounds=round_num + 1))
                    elif error == _NO_STATS:
                        # GitHub confirmed no stats exist — treat as empty, don't retry
                        yr = compute_yearly_breakdown([], year_start, year_end,
                                                       threshold=threshold, base=base,
                                                       include_bots=include_bots)
                        rr = _RepoResult(repo=repo, year_results=yr,
                                         elapsed=repo_api_time[repo], rounds=round_num + 1)
                        results.append(rr)
                        pending_flush.append((rr.repo, rr.year_results))
                    elif data is None:
                        # Deferred (202/204) — retry next round
                        deferred.append(repo)
                        continue
                    else:
                        yr = compute_yearly_breakdown(data, year_start, year_end,
                                                       threshold=threshold, base=base,
                                                       include_bots=include_bots)
                        rr = _RepoResult(repo=repo, year_results=yr,
                                         elapsed=repo_api_time[repo], rounds=round_num + 1)
                        results.append(rr)
                        pending_flush.append((rr.repo, rr.year_results))

                    if len(pending_flush) >= FLUSH_EVERY:
                        _upsert_yearly_csv_batch(output, pending_flush)
                        pending_flush.clear()

                    label = repo[:name_width].ljust(name_width)
                    progress.update(task, completed=len(results),
                                    description=f"{label} ({len(results)}/{len(to_fetch)})")

                pending_repos = deferred
                if deferred and round_num < MAX_ROUNDS - 1:
                    log.info("Round %d done: %d resolved, %d deferred → retrying",
                             round_num + 1, len(results), len(deferred))

            # Any repos still deferred after all rounds → treat as empty
            for repo in pending_repos:
                yr = compute_yearly_breakdown([], year_start, year_end,
                                               threshold=threshold, base=base,
                                               include_bots=include_bots)
                rr = _RepoResult(repo=repo, year_results=yr,
                                 elapsed=repo_api_time[repo], rounds=MAX_ROUNDS)
                results.append(rr)
                pending_flush.append((rr.repo, rr.year_results))
                log.warning("%s: no stats after %d rounds, treating as empty", repo, MAX_ROUNDS)

            # Flush remaining
            if pending_flush:
                _upsert_yearly_csv_batch(output, pending_flush)
                pending_flush.clear()

    # Results table
    errors = sum(1 for r in results if r.year_results is None)
    elapsed = time.monotonic() - t_start

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Repo", min_width=20)
    table.add_column("BF", justify="right")
    table.add_column("HHI", justify="right")
    table.add_column("Contribs", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("Time", justify="right", style="dim")

    # Sort by original order
    results.sort(key=lambda r: to_fetch.index(r.repo))

    for i, rr in enumerate(results):
        is_last = i == len(results) - 1
        if rr.year_results is None:
            table.add_row(rr.repo, "[red]error[/red]", "", "", "", f"{rr.elapsed:.1f}s",
                          end_section=is_last)
            continue
        total_r = next((r for label, r in rr.year_results if label == total_label), None)
        if total_r is None:
            continue
        humans = [c for c in total_r.contributors if not c.is_bot]
        total_commits = sum(c.commits for c in humans)
        table.add_row(
            rr.repo,
            f"[yellow bold]{total_r.bus_factor}[/yellow bold]" if total_r.bus_factor > 0 else "[dim]–[/dim]",
            f"{round(total_r.hhi * 10000):,}" if total_r.hhi > 0 else "[dim]–[/dim]",
            _fmt_contribs_label(total_r.contributors) if humans else "[dim]–[/dim]",
            f"{total_commits:,}" if total_commits else "[dim]–[/dim]",
            f"{rr.elapsed:.1f}s",
            end_section=is_last,
        )

    # Summary footer
    ok_results = [r for r in results if r.year_results is not None]
    times = [r.elapsed for r in results]
    avg_time = sum(times) / len(times) if times else 0
    max_time = max(times) if times else 0
    min_time = min(times) if times else 0

    table.add_row(
        f"[dim]{len(ok_results)} repos[/dim]",
        "", "", "", "",
        f"[bold]{elapsed:.1f}s[/bold]",
    )
    table.add_row(
        "[dim]avg / min / max[/dim]",
        "", "", "", "",
        f"[dim]{avg_time:.1f} / {min_time:.1f} / {max_time:.1f}s[/dim]",
    )
    table.add_row(
        "[dim]API calls[/dim]",
        "", "", "", "",
        f"[dim]{limiter.requests_made}[/dim]",
    )
    table.add_row(
        "[dim]skipped / errors[/dim]",
        "", "", "", "",
        f"[dim]{skipped} / {errors}[/dim]",
    )

    console.print()
    console.print(table)

    # Print problematic repos (errors or 3+ rounds)
    problem_repos = [r for r in results if r.error or r.rounds >= 3]
    if problem_repos:
        console.print()
        ptable = Table(show_header=True, header_style="bold dim", padding=(0, 1),
                       title="[bold]Problematic repos[/bold]")
        ptable.add_column("Repo", min_width=20)
        ptable.add_column("Rounds", justify="right")
        ptable.add_column("Issue")
        for rr in problem_repos:
            if rr.error:
                issue = f"[red]{rr.error}[/red]"
            elif rr.rounds >= MAX_ROUNDS:
                issue = "[yellow]no stats after all rounds[/yellow]"
            else:
                issue = f"[dim]resolved after {rr.rounds} rounds[/dim]"
            ptable.add_row(rr.repo, str(rr.rounds), issue)
        console.print(ptable)


def _load_repos_from_csv(filepath: str, top: int | None = None) -> list[str]:
    """Load repo slugs from a CSV file (expects 'repo' column).

    Skips archived repos if the CSV has an 'archived' column.
    Sorts by stars descending if the CSV has a 'stars' column.
    """
    entries: list[tuple[str, int]] = []  # (repo, stars)
    skipped_archived = 0
    with open(filepath, encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        fields = reader.fieldnames or []
        has_archived = "archived" in fields
        has_stars = "stars" in fields
        for row in reader:
            if has_archived and row.get("archived", "").strip().lower() in ("true", "1"):
                skipped_archived += 1
                continue
            repo = row.get("repo", "").strip().lower()
            if repo:
                stars = int(row.get("stars", 0) or 0) if has_stars else 0
                entries.append((repo, stars))
    if skipped_archived:
        log.info("Skipped %d archived repos", skipped_archived)
        console.print(f"[dim]Skipped {skipped_archived} archived repos[/dim]")
    if has_stars:
        entries.sort(key=lambda e: e[1], reverse=True)
    if top:
        entries = entries[:top]
    return [repo for repo, _ in entries]


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate bus factor for a GitHub repo")
    parser.add_argument("repo", nargs="?", help="GitHub repo URL or owner/repo slug")
    parser.add_argument("--repos", help="CSV file with repos to batch update (expects 'repo' column)")
    parser.add_argument("--top", type=int, help="Only process first N repos from --repos CSV")
    parser.add_argument("--base", choices=["commits", "locs"], default="commits",
                        help="Metric for bus factor (default: commits)")
    parser.add_argument("--source", choices=["github", "git"], default="github",
                        help="Data source: github API or git clone (default: github)")
    parser.add_argument("--token", help="GitHub personal access token")
    parser.add_argument("--threshold", type=float, default=THRESHOLD, help="Ownership threshold (default 0.5)")
    parser.add_argument("--year", type=int, help="Filter to a specific year (e.g. 2024)")
    parser.add_argument("--year-end", type=int, help="End year for range (e.g. --year 2022 --year-end 2024)")
    parser.add_argument("--years", type=int, nargs=2, metavar=("START", "END"),
                        help="Year range with breakdown (e.g. --years 2020 2025)")
    parser.add_argument("--since", help="Start date YYYY-MM-DD (e.g. 2024-06-01)")
    parser.add_argument("--until", help="End date YYYY-MM-DD (e.g. 2025-01-31)")
    parser.add_argument("--output", help="CSV file to upsert yearly metrics into")
    parser.add_argument("--include-bots", action="store_true", default=False,
                        help="Include bots as regular contributors in bus factor calculation")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if not args.repo and not args.repos:
        parser.error("either repo or --repos is required")
    if args.repos and args.repo:
        parser.error("cannot use both repo and --repos")
    if args.repos and not args.years:
        parser.error("--repos requires --years")
    if args.repos and not args.output:
        parser.error("--repos requires --output")
    if args.top and not args.repos:
        parser.error("--top requires --repos")
    if args.year_end and not args.year:
        parser.error("--year-end requires --year")
    if (args.since or args.until) and (args.year or args.years):
        parser.error("--since/--until and --year/--years are mutually exclusive")
    if args.years and args.year:
        parser.error("--years and --year are mutually exclusive")

    if args.verbose:
        logging.getLogger(__name__).setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    load_dotenv()
    token = args.token or os.environ.get("GITHUB_TOKEN")

    # Batch mode
    if args.repos:
        repos = _load_repos_from_csv(args.repos, top=args.top)
        asyncio.run(batch_update(
            repos, args.years[0], args.years[1], args.output,
            threshold=args.threshold, base=args.base,
            include_bots=args.include_bots, token=token,
        ))
        return

    if args.years:
        date_range = DateRange.from_years(args.years[0], args.years[1])
    elif args.year:
        date_range = DateRange.from_years(args.year, args.year_end)
    elif args.since or args.until:
        date_range = DateRange.from_dates(args.since, args.until)
    else:
        date_range = DateRange()

    repo = parse_repo(args.repo)

    if args.years:
        # Year range with breakdown: fetch once, show yearly table + contributor table
        t_start = time.monotonic()
        with _spinner("Fetching contributor stats from GitHub API..."):
            stats = fetch_contributor_stats(repo, token=token)
        api_time = time.monotonic() - t_start

        year_results = compute_yearly_breakdown(stats, args.years[0], args.years[1],
                                                threshold=args.threshold, base=args.base,
                                                include_bots=args.include_bots)
        display_yearly_breakdown(repo, year_results, base=args.base)

        if args.output:
            _upsert_yearly_csv(args.output, repo, year_results)

        # Show contributor table for the total range (skip summary — it's in the yearly table)
        _, total_result = year_results[-1]
        total_result.perf = PerfStats(
            source="api", api_fetch=api_time, total=time.monotonic() - t_start,
            commits_analyzed=sum(c.commits for c in total_result.contributors if not c.is_bot),
            contributors_found=len(total_result.contributors),
        )
        display_results(repo, total_result, date_range=date_range, base=args.base, skip_summary=True)
    else:
        result = run(repo, token=token, date_range=date_range, threshold=args.threshold,
                     base=args.base, source=args.source, include_bots=args.include_bots)
        display_results(repo, result, date_range=date_range, base=args.base)


if __name__ == "__main__":
    main()
