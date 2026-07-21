"""Contributor metrics — lifetime bus factor + HHI from GitHub's
/repos/{repo}/contributors endpoint.

Contributors are keyed by GitHub login. The /stats/contributors endpoint
(per-week, time-windowed) is intentionally NOT used — it returns HTTP 202
"computing" indefinitely for most repos, so every metric here is a
lifetime aggregate at fetch time.

Usage:
    python -m src.sources.github.fetch_contributors_metrics facebook/react   # one repo
    python -m src.sources.github.fetch_contributors_metrics                  # batch: value-data.csv A/B repos
    python -m src.sources.github.fetch_contributors_metrics --refresh        # ignore the TTL, refetch all

Caching: a repo whose status row in contributor-commits.status.csv is younger
than `TTL_DAYS` is skipped, so a warm re-run makes zero API calls. A repo that
is missing, stale, or whose last fetch ERRORED is re-fetched — an error row is
never treated as fresh, so a failed fetch can never masquerade as a genuine
"no contributors" result.
"""

import argparse
import asyncio
import csv
import logging
import os
import re

from src.sources.github.batch_runner import GH_CONTRIB_STATUS_FILE, batch_update
from src.sources.github.display import console
from src.sources.github.models import (
    Contributor, PerfStats, RunResult,
    THRESHOLD, is_bot,
)
from src.common.freshness import row_is_fresh
from src.common.params import fetch_ttl_days
from src.common.repos import VALUE_FILE, load_github_top_slugs

# Contributor mixes move slowly, and the /contributors payload is the most
# expensive fetch in this source (paginated, one round-trip per page per repo),
# so a repo is re-fetched at most once a year. The TTL (365 days) comes from
# settings.json. `--refresh` overrides it.
TTL_DAYS = fetch_ttl_days("sources/github/fetch_contributors_metrics")


def fresh_repos(ttl_days: int = TTL_DAYS) -> set[str]:
    """Repos in contributor-commits.status.csv fetched within `ttl_days`.

    Gated through the shared `row_is_fresh` helper with the sidecar's `status`
    column, so a row recording an errored fetch is never fresh and is always
    retried. A "" / "empty" status IS a real measurement (the repo genuinely
    has no visible contributors) and stays cached for the full TTL.
    """
    if not os.path.exists(GH_CONTRIB_STATUS_FILE):
        return set()
    with open(GH_CONTRIB_STATUS_FILE, encoding="utf-8") as f:
        return {
            repo
            for row in csv.DictReader(f)
            if (repo := (row.get("repo") or "").strip())
            and row_is_fresh(row, ttl_days, status_key="status")
        }


def parse_repo(url_or_slug: str) -> str:
    """Extract 'owner/repo' from a GitHub URL or slug."""
    url_or_slug = url_or_slug.strip().rstrip("/")
    m = re.match(r"(?:https?://github\.com/)?([^/]+/[^/]+?)(?:\.git)?$", url_or_slug)
    if not m:
        raise ValueError(f"Cannot parse repo from: {url_or_slug}")
    return m.group(1).lower()


def _compute_bus_factor(
    contributors: list[Contributor], threshold: float = THRESHOLD,
    base: str = "commits", include_bots: bool = False,
) -> tuple[int, list[Contributor], float]:
    """Core bus factor + HHI computation from a list of Contributors.

    Both metrics are measured over the *non-bot* contributors we can see,
    and each contributor's share is taken against the sum of those same
    contributors' contributions. Numerator and denominator must come from
    the one population: using an external total (e.g. the lifetime
    /commits count, which also includes bot commits) deflates HHI below
    its mathematical floor and inflates the bus factor toward the full
    contributor count for any repo with bot activity.
    """
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


def compute_lifetime_metrics(
    contributors_data: list[dict],
    total_commits: int | None = None,
    total_contributors: int | None = None,
    label: str = "2021-2025",
    threshold: float = THRESHOLD,
    include_bots: bool = False,
) -> list[tuple[str, RunResult]]:
    """Compute BF/HHI from /repos/{owner}/{repo}/contributors output.

    `total_commits` and `total_contributors` are recorded on the
    RunResult for downstream reporting but do NOT affect BF/HHI — those
    are computed purely from the visible non-bot contributors' own commit
    shares (see `_compute_bus_factor`).
    """
    contributors: list[Contributor] = []
    for c in contributors_data:
        login = (c.get("login") or "").lower()
        is_b = c.get("type") == "Bot" or is_bot(login)
        contributors.append(
            Contributor(
                login=login,
                lines_changed=0,
                commits=int(c.get("contributions") or 0),
                is_bot=is_b,
            )
        )
    bf, sorted_c, hhi = _compute_bus_factor(
        contributors, threshold=threshold, base="commits",
        include_bots=include_bots,
    )
    return [(label, RunResult(
        bus_factor=bf, contributors=sorted_c, hhi=hhi,
        perf=PerfStats(source="contributors-api"),
        total_commits=total_commits,
        total_contributors=total_contributors,
    ))]


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate bus factor for a GitHub repo")
    parser.add_argument("repo", nargs="?", help="GitHub repo URL or owner/repo slug")
    parser.add_argument("--input", default=VALUE_FILE,
                        help=f"value-data CSV (default: {VALUE_FILE} — "
                             f"loads A/B class repos with non-empty github_repo, skips archived)")
    parser.add_argument("--limit", type=int, help="Process N random repos from --input CSV")
    parser.add_argument("--threshold", type=float, default=THRESHOLD,
                        help="Ownership threshold (default 0.5)")
    parser.add_argument("--include-bots", action="store_true", default=False,
                        help="Include bots as regular contributors in bus factor calculation")
    parser.add_argument("--refresh", "--force", action="store_true", dest="refresh",
                        help=f"Re-fetch all repos, ignoring the {TTL_DAYS}-day freshness gate")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.limit and args.repo:
        parser.error("--limit requires batch mode (no repo argument)")

    if args.verbose:
        logging.getLogger("src.sources.github").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Single-repo mode: fetch just that repo (always forced — an explicit
    # ad-hoc request should not be skipped by the freshness gate).
    if args.repo:
        repo = parse_repo(args.repo)
        asyncio.run(batch_update(
            [repo], threshold=args.threshold,
            include_bots=args.include_bots, force=True,
        ))
        return

    # Batch mode: every risk-scope GitHub repo. The GitHub /contributors API
    # can't service a GitLab target (a gl/ slug would resolve to an unrelated
    # GitHub mirror), so non-GitHub repos are excluded here — the concentration
    # builder's GitHub columns simply stay blank for them (the git-clone method
    # supplies their score).
    repos = load_github_top_slugs(value_file=args.input)

    # Freshness gate lives here, not in batch_update: this is the layer that
    # owns the TTL policy, and it is the only caller. `to_fetch` is already
    # gated, so batch_update is invoked with force=True to stop it re-applying
    # its own (redundant) filter on top.
    fresh = set() if args.refresh else fresh_repos(TTL_DAYS)
    to_fetch = [r for r in repos if r not in fresh]
    console.print(
        f"[bold]contributors[/bold]: {len(repos)} repos, {len(to_fetch)} to fetch, "
        f"{len(repos) - len(to_fetch)} fresh (< {TTL_DAYS}d)"
    )

    if not to_fetch:
        console.print(f"[dim]All repos fresh (< {TTL_DAYS}d) — nothing to fetch; "
                      f"--refresh to force.[/dim]")
        return

    asyncio.run(batch_update(
        to_fetch, threshold=args.threshold,
        include_bots=args.include_bots, limit=args.limit, force=True,
    ))


if __name__ == "__main__":
    main()
