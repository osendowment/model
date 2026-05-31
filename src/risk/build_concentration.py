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
        total_commits_github,              (sum of /contributors `contributions`)
        total_contributors_github,         (all /contributors rows, incl. bots)
        active_contributors_github,        (non-bot /contributors rows)
        bf_commits_github,                 (bus factor over non-bot, GitHub method)
        hhi_commits_github,                (HHI 0-10000 over non-bot, GitHub method)
        total_commits_git,                 (lifetime non-merge commits, git method)
        contributors_git,                  (merged non-bot identities, lifetime)
        bf_commits_git,                    (bus factor, git method, lifetime)
        hhi_commits_git,                   (HHI 0-10000, git method, lifetime)
        commits_git_2021_2025,             (non-merge commits in 2021-2025)
        active_contributors_git_2021_2025, (merged non-bot identities active 2021-2025 — the AC)
        bf_commits_git_2021_2025,          (bus factor, git method, 2021-2025 window)
        hhi_commits_git_2021_2025,         (HHI 0-10000, git method, 2021-2025 window)
        concentration_class,               (A/B/C/D from bf_commits_git + hhi_commits_git)
        github_fetched_at, git_fetched_at

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

from src.sources.git.contributors import _is_bot_identity, merge_identity_groups
from src.sources.github.fetch_contributors_metrics import _compute_bus_factor
from src.sources.github.models import Contributor, is_bot
from src.common.percentiles import add_percentiles
from src.common.repos import load_risk_repos
from src.common.tables import load_rows_by_repo

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
GIT_LONG_FILE = DATA_DIR / "sources" / "git" / "contributor-commits.csv"
GIT_STATUS_FILE = DATA_DIR / "sources" / "git" / "contributor-commits.status.csv"
GH_LONG_FILE = DATA_DIR / "sources" / "github" / "contributor-commits.csv"
GH_STATUS_FILE = DATA_DIR / "sources" / "github" / "contributor-commits.status.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "concentration.csv"

WINDOW = range(2021, 2026)  # 2021..2025 inclusive

FIELDS = [
    "repo", "repo_id",
    "total_commits_github", "total_contributors_github", "active_contributors_github",
    "bf_commits_github", "bf_commits_github_p",
    "hhi_commits_github", "hhi_commits_github_p",
    "total_commits_git", "contributors_git",
    "bf_commits_git", "bf_commits_git_p",
    "hhi_commits_git", "hhi_commits_git_p",
    "commits_git_2021_2025", "active_contributors_git_2021_2025",
    "bf_commits_git_2021_2025", "bf_commits_git_2021_2025_p",
    "hhi_commits_git_2021_2025", "hhi_commits_git_2021_2025_p",
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


def _bus_factor_hhi(commit_counts: list[int]) -> tuple[int, int]:
    """Bus factor + HHI (0-10000) over a list of per-contributor commit counts.

    Reuses the shared `_compute_bus_factor` (login is irrelevant here — bots
    are already dropped, so `include_bots=True` stops it re-filtering).
    """
    objs = [Contributor(login="", lines_changed=0, commits=c)
            for c in commit_counts if c > 0]
    bf, _sorted, hhi = _compute_bus_factor(objs, include_bots=True)
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
        "total_commits_github": sum(c for _l, c, _t in parsed),
        "total_contributors_github": len(parsed),
        "active_contributors_github": len(nonbot),
        "bf_commits_github": bf,
        "hhi_commits_github": hhi,
    }


def git_metrics(rows: list[dict]) -> dict:
    """Concentration metrics for one repo from the git contributor-commit rows.

    Identities (raw name+email pairs) are union-find merged; bots dropped;
    bus factor / HHI / contributor counts computed for both the full lifetime
    and the 2021-2025 window.
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

    total_life = total_win = 0
    life_humans: list[int] = []   # per merged non-bot person, lifetime commits
    win_humans: list[int] = []    # per merged non-bot person, 2021-2025 commits
    for group in merge_identity_groups(pairs):
        names = [pairs[i][0] for i in group]
        emails = [pairs[i][1] for i in group]
        life = win = 0
        for i in group:
            for year, commits in by_identity[pairs[i]].items():
                life += commits
                if year in WINDOW:
                    win += commits
        total_life += life
        total_win += win
        if _is_bot_identity(names, emails):
            continue
        life_humans.append(life)
        if win > 0:
            win_humans.append(win)

    bf_l, hhi_l = _bus_factor_hhi(life_humans)
    bf_w, hhi_w = _bus_factor_hhi(win_humans)
    return {
        "total_commits_git": total_life,
        "contributors_git": len(life_humans),
        "bf_commits_git": bf_l,
        "hhi_commits_git": hhi_l,
        "commits_git_2021_2025": total_win,
        "active_contributors_git_2021_2025": len(win_humans),
        "bf_commits_git_2021_2025": bf_w,
        "hhi_commits_git_2021_2025": hhi_w,
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
            ("bf_commits_github", False), ("hhi_commits_github", True),
            ("bf_commits_git", False), ("hhi_commits_git", True),
            ("bf_commits_git_2021_2025", False), ("hhi_commits_git_2021_2025", True),
        ],
        composite_cols=["bf_commits_git_2021_2025_p", "hhi_commits_git_2021_2025_p"],
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
        "bf_commits_github", "hhi_commits_github", "active_contributors_github",
        "bf_commits_git", "hhi_commits_git", "contributors_git",
        "active_contributors_git_2021_2025", "bf_commits_git_2021_2025",
        "score",
    ):
        n = sum(1 for r in rows if str(r[col]).strip())
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
