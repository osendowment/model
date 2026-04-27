"""Data models for contributor metrics."""
from __future__ import annotations

import datetime
from dataclasses import dataclass


THRESHOLD = 0.5  # 50% of total work

# Known bot accounts — excluded from bus factor calculation
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


def is_bot(login: str) -> bool:
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
    total_loc: int | None = None
    # Exact totals from /commits and /contributors Link-header tricks. When
    # `total_commits` is set, BF/HHI use it as denominator (more accurate
    # than sum-of-visible-contributions for repos with >500 contributors,
    # whose long tail is silently truncated by /contributors).
    total_commits: int | None = None
    total_contributors: int | None = None


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
            if self.since.month == 1 and self.since.day == 1 and self.until.month == 12 and self.until.day == 31:
                if self.since.year == self.until.year:
                    return str(self.since.year)
                return f"{self.since.year}–{self.until.year}"
            return f"{self.since} to {self.until}"
        if self.since:
            return f"since {self.since}"
        return f"until {self.until}"
