"""Contributor metrics — lifetime bus factor + HHI from GitHub's
/repos/{repo}/contributors endpoint.

Contributors are keyed by GitHub login. The /stats/contributors endpoint
(per-week, time-windowed) is intentionally NOT used — it returns HTTP 202
"computing" indefinitely for most repos, so every metric here is a
lifetime aggregate at fetch time.

Usage:
    python -m src.github.fetch_contributors_metrics facebook/react   # one repo
    python -m src.github.fetch_contributors_metrics                  # batch: value-data.csv A/B repos
"""

import argparse
import asyncio
import logging
import re

from src.github.batch_runner import batch_update
from src.github.models import (
    Contributor, PerfStats, RunResult,
    THRESHOLD, is_bot,
)
from src.pipeline.common.repos import VALUE_FILE, load_risk_slugs


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
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch all repos, ignoring the per-repo freshness gate")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.limit and args.repo:
        parser.error("--limit requires batch mode (no repo argument)")

    if args.verbose:
        logging.getLogger("src.github").setLevel(logging.DEBUG)
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

    # Batch mode: every A/B-class repo from value-data.csv.
    repos = load_risk_slugs(value_file=args.input)
    asyncio.run(batch_update(
        repos, threshold=args.threshold,
        include_bots=args.include_bots, limit=args.limit, force=args.force,
    ))


if __name__ == "__main__":
    main()
