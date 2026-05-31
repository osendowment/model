#!/usr/bin/env python3
"""Build data/risk/concentration.csv — contributor-concentration metrics per risk-scope repo.

Two independent methods measure contributor concentration, each with its own
raw long-format source. This builder merges identities, drops bots, and
computes bus factor / HHI / contributor counts from both:

Reads:
    data/value/value.csv                          — A/B value-class set (load_risk_repos)
    data/sources/git/contributor-commits.csv             — git-clone method, long raw:
                                                   repo, author_name, author_email, year, commits
    data/sources/git/contributor-commits.status.csv      — per-repo git fetch status / fetched_at
    data/sources/github/contributor-commits.csv          — GitHub /contributors method, long raw:
                                                   repo, login, contributions, account_type
    data/sources/github/contributor-commits.status.csv   — per-repo GitHub fetch status / fetched_at

The git method walks `git log` on a local clone — it sees every contributor
and carries author dates, so it yields both a lifetime figure and a windowed
2021-2025 figure. The GitHub `/contributors` method is lifetime-only and caps
the contributor list near 500, so it under-counts big repos; it is kept for
cross-checking.

Writes:
    data/risk/concentration.csv  with columns:
        repo, repo_id,
        total_commits_gh_alltime,          (sum of /contributors `contributions`)
        total_contributors_gh_alltime,     (all /contributors rows, incl. bots)
        active_contributors_gh_alltime,    (non-bot /contributors rows)
        bf_commits_gh_alltime,             (bus factor over non-bot, GitHub method)
        hhi_commits_gh_alltime,            (HHI 0-10000 over non-bot, GitHub method)
        total_commits_git_full,            (non-merge commits through last complete year)
        contributors_git_full,             (merged non-bot identities, _full)
        bf_commits_git_full,               (bus factor, git method, _full)
        hhi_commits_git_full,              (HHI 0-10000, git method, _full)
        commits_git_5y,                    (non-merge commits in the _5y window)
        active_contributors_git_5y,        (merged non-bot identities active in _5y — the AC)
        bf_commits_git_5y,                 (bus factor, git method, _5y window)
        hhi_commits_git_5y,                (HHI 0-10000, git method, _5y window)
        score,                             (0-100 concentration risk = geometric mean of the
                                            _5y bf + hhi risk percentiles)
        github_fetched_at, git_fetched_at

Periods (year-agnostic column names; the boundary lives in settings.json):
    _full    = all commits through the last complete year = max(settings `years`).
    _5y      = the last `concentration.window_years` complete years, anchored to
               that same last complete year (currently 2021-2025).
    The GitHub `/contributors` API exposes only a cumulative lifetime count with
    no per-year breakdown, so its columns are `_gh_alltime` — an uncapped
    all-time figure as of `github_fetched_at` (may include the partial current
    year), deliberately NOT labelled `_full`/`_5y` which it cannot honour.
    Only the git `_5y` axis feeds `score`.

Identity merging (git method): the repo's own `.mailmap` is already applied at
fetch time (`%aN`/`%aE`); this builder additionally union-finds identities that
share a normalised email or a full name (`merge_identity_groups`). The GitHub
method needs no merging — `/contributors` is keyed by GitHub account.

Usage:
    uv run python -m src.risk.build_concentration
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.params import (
    BUS_FACTOR_THRESHOLD,
    CONCENTRATION_WINDOW_YEARS,
    LAST_COMPLETE_YEAR,
)
from src.common.percentiles import add_percentiles
from src.common.repos import load_risk_repos
from src.common.tables import load_rows_by_repo
from src.sources.git.contributors import _is_bot_identity, merge_identity_groups
from src.sources.github.fetch_contributors_metrics import _compute_bus_factor
from src.sources.github.models import Contributor, is_bot

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
GIT_LONG_FILE = DATA_DIR / "sources" / "git" / "contributor-commits.csv"
GIT_STATUS_FILE = DATA_DIR / "sources" / "git" / "contributor-commits.status.csv"
GH_LONG_FILE = DATA_DIR / "sources" / "github" / "contributor-commits.csv"
GH_STATUS_FILE = DATA_DIR / "sources" / "github" / "contributor-commits.status.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "concentration.csv"

# _full caps at LAST_COMPLETE_YEAR (settings `max(years)`); the _5y window is the
# last CONCENTRATION_WINDOW_YEARS complete years anchored to it. Both live in
# settings.json so the schema below stays year-agnostic.
WINDOW = range(LAST_COMPLETE_YEAR - CONCENTRATION_WINDOW_YEARS + 1, LAST_COMPLETE_YEAR + 1)

FIELDS = [
    "repo", "repo_id",
    "total_commits_gh_alltime", "total_contributors_gh_alltime", "active_contributors_gh_alltime",
    "bf_commits_gh_alltime", "bf_commits_gh_alltime_p",
    "hhi_commits_gh_alltime", "hhi_commits_gh_alltime_p",
    "total_commits_git_full", "contributors_git_full",
    "bf_commits_git_full", "bf_commits_git_full_p",
    "hhi_commits_git_full", "hhi_commits_git_full_p",
    "commits_git_5y", "active_contributors_git_5y",
    "bf_commits_git_5y", "bf_commits_git_5y_p",
    "hhi_commits_git_5y", "hhi_commits_git_5y_p",
    "score",
    "github_fetched_at", "git_fetched_at",
]



def _load_long_grouped(path: Path) -> dict[str, list[dict]]:
    """Read a long CSV → ``{repo_lowercased: [row, ...]}`` (missing file → {})."""
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out.setdefault(slug, []).append(row)
    return out


def _int(value: object) -> int:
    """Parse a CSV cell to int. Empty / unparseable → 0."""
    s = str(value if value is not None else "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _bus_factor_hhi(commit_counts: list[int]) -> tuple[int | str, int | str]:
    """Bus factor + HHI (0-10000) over a list of per-contributor commit counts.

    Reuses the shared `_compute_bus_factor` (login is irrelevant here — bots
    are already dropped, so `include_bots=True` stops it re-filtering).

    Returns ("", "") when there is no contributor with a positive commit
    count: bus factor and HHI are *undefined* for an empty population, not
    zero. Emitting a real 0 would be a lack-of-data value masquerading as a
    measurement — and worse, it would rank as maximum concentration risk
    (bus factor 0 < 1) while simultaneously ranking as minimum HHI risk.
    A blank keeps the repo out of both percentile rankings.
    """
    objs = [Contributor(login="", lines_changed=0, commits=c)
            for c in commit_counts if c > 0]
    if not objs:
        return "", ""
    bf, _sorted, hhi = _compute_bus_factor(
        objs, threshold=BUS_FACTOR_THRESHOLD, include_bots=True)
    return bf, round(hhi * 10000)


def github_metrics(rows: list[dict]) -> dict:
    """Concentration metrics for one repo from the GitHub /contributors rows."""
    parsed = []
    for r in rows:
        login = (r.get("login") or "").strip().lower()
        parsed.append((login, _int(r.get("contributions")),
                       (r.get("account_type") or "").strip()))
    nonbot = [(login, c) for login, c, atype in parsed
              if atype != "Bot" and not is_bot(login)]
    bf, hhi = _bus_factor_hhi([c for _login, c in nonbot])
    return {
        "total_commits_gh_alltime": sum(c for _l, c, _t in parsed),
        "total_contributors_gh_alltime": len(parsed),
        "active_contributors_gh_alltime": len(nonbot),
        "bf_commits_gh_alltime": bf,
        "hhi_commits_gh_alltime": hhi,
    }


def git_metrics(rows: list[dict]) -> dict:
    """Concentration metrics for one repo from the git contributor-commit rows.

    Identities (raw name+email pairs) are union-find merged; bots dropped;
    bus factor / HHI / contributor counts computed for both the _full period
    (all commits through LAST_COMPLETE_YEAR) and the _5y window. Commits in a
    not-yet-complete year (> LAST_COMPLETE_YEAR) are excluded from both, so the
    figures are stable within a year and reproducible across reruns.
    """
    # {(name, email): {year: commits}}
    by_identity: dict[tuple[str, str], dict[int, int]] = {}
    for r in rows:
        name = (r.get("author_name") or "").strip()
        email = (r.get("author_email") or "").strip()
        year = _int(r.get("year"))
        commits = _int(r.get("commits"))
        years = by_identity.setdefault((name, email), {})
        years[year] = years.get(year, 0) + commits

    pairs = list(by_identity)
    if not pairs:
        return {}

    total_full = total_win = 0
    full_humans: list[int] = []   # per merged non-bot person, _full commits
    win_humans: list[int] = []    # per merged non-bot person, _5y commits
    for group in merge_identity_groups(pairs):
        names = [pairs[i][0] for i in group]
        emails = [pairs[i][1] for i in group]
        full = win = 0
        for i in group:
            for year, commits in by_identity[pairs[i]].items():
                if year <= LAST_COMPLETE_YEAR:   # _full caps at the last complete year
                    full += commits
                if year in WINDOW:
                    win += commits
        total_full += full
        total_win += win
        if _is_bot_identity(names, emails):
            continue
        if full > 0:
            full_humans.append(full)
        if win > 0:
            win_humans.append(win)

    bf_l, hhi_l = _bus_factor_hhi(full_humans)
    bf_w, hhi_w = _bus_factor_hhi(win_humans)
    return {
        "total_commits_git_full": total_full,
        "contributors_git_full": len(full_humans),
        "bf_commits_git_full": bf_l,
        "hhi_commits_git_full": hhi_l,
        "commits_git_5y": total_win,
        "active_contributors_git_5y": len(win_humans),
        "bf_commits_git_5y": bf_w,
        "hhi_commits_git_5y": hhi_w,
    }


def build() -> list[dict]:
    eligible = load_risk_repos()
    git_long = _load_long_grouped(GIT_LONG_FILE)
    gh_long = _load_long_grouped(GH_LONG_FILE)
    git_status = load_rows_by_repo(GIT_STATUS_FILE)
    gh_status = load_rows_by_repo(GH_STATUS_FILE)

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        row = {f: "" for f in FIELDS}
        row["repo"] = repo
        row["repo_id"] = entry.repo_id

        if repo in gh_long:
            row.update(github_metrics(gh_long[repo]))
        row["github_fetched_at"] = (
            gh_status.get(repo, {}).get("fetched_at") or ""
        ).strip()

        if repo in git_long:
            row.update(git_metrics(git_long[repo]))
        row["git_fetched_at"] = (
            git_status.get(repo, {}).get("fetched_at") or ""
        ).strip()

        rows.append(row)
    add_percentiles(
        rows,
        pctl_specs=[
            ("bf_commits_gh_alltime", False), ("hhi_commits_gh_alltime", True),
            ("bf_commits_git_full", False), ("hhi_commits_git_full", True),
            ("bf_commits_git_5y", False), ("hhi_commits_git_5y", True),
        ],
        composite_cols=["bf_commits_git_5y_p", "hhi_commits_git_5y_p"],
        dim_col="score",
    )
    return rows


def main() -> None:
    console.print("[bold]Building concentration.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Concentration coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in (
        "bf_commits_gh_alltime", "hhi_commits_gh_alltime", "active_contributors_gh_alltime",
        "bf_commits_git_full", "hhi_commits_git_full", "contributors_git_full",
        "active_contributors_git_5y", "bf_commits_git_5y",
        "score",
    ):
        n = sum(1 for r in rows if str(r[col]).strip())
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
