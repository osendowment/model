"""Fetch CHAOSS-aligned per-repo metrics from OSSInsight (GitHub Archive aggregate).

OSSInsight (https://ossinsight.io) aggregates the full GH Archive event stream
into TiDB and exposes named SQL queries via a free public API. Per-query base
endpoint: ``https://api.ossinsight.io/q/<query-name>?repoId=<numeric-id>``.

We hit a handful of these per repo and roll the month-by-month series into a
5-year window in Python:

  * ``analyze-repo-overview``                — lifetime stars/commits/issues/PR creators
  * ``analyze-repo-pr-overview``             — lifetime PR / PR creator / review counts
  * ``analyze-repo-issue-overview``          — lifetime issue / commenter counts
  * ``analyze-pushes-and-commits-per-month`` — month-by-month commits  → ``commits_5y``
  * ``analyze-stars-history``                — month-by-month total stars (cumulative)
  * ``analyze-pull-request-open-to-merged``  — month-by-month percentile merge time
  * ``analyze-issue-open-to-closed``         — month-by-month percentile close time
  * ``analyze-pull-requests-size-per-month`` — month-by-month opened-PR counts (``all_size``)
  * ``analyze-issue-opened-and-closed``      — month-by-month opened/closed issue counts
  * ``analyze-repo-top-contributors``        — top-N contributors by event count (lifetime)
  * ``analyze-recent-pull-requests``         — 28-day-window PR throughput (informational)

Numeric ``repo_id`` is taken from ``data/eligibility-data.csv`` when present;
otherwise resolved via OSSInsight's GitHub proxy (``/gh/repo/{owner}/{repo}``).

Output: ``data/ossinsight/repos.csv`` (one row per repo).

Gaps (not available from OSSInsight's free public queries):
  * ``unique_committers_5y`` — no per-repo PushEvent contributor count by window
  * ``forks_growth_5y``      — ForkEvent counts aren't exposed as a cumulative series
  * ``prs_merged_5y`` / ``prs_closed_5y`` — no monthly merged/closed PR counts
    (only the percentile distributions of merge time, not raw counts)
  * ``active_contributors_5y`` is **lifetime** (count of distinct contributors
    across all of GH-Archive history), not strictly the last 5y. The only
    public time-windowed contributor count is the 28-day rolling figure in
    ``analyze-recent-contributors``.
All gaps are written empty (or marked above); computing them requires either a
custom SQL playground query (heavily rate-limited) or BigQuery against the
``githubarchive`` public dataset.

Usage:
    uv run python -m src.ossinsight.fetch --limit 5
    uv run python -m src.ossinsight.fetch --limit 5 --concurrency 4
    uv run python -m src.ossinsight.fetch  # all eligible repos
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

import aiohttp
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from src.pipeline.repos import load_eligible_repos

log = logging.getLogger(__name__)
console = Console()

OSSINSIGHT_API = "https://api.ossinsight.io"
DEFAULT_OUTPUT = Path("data/ossinsight/repos.csv")
RAW_CACHE_DIR = Path("data/ossinsight/raw")

REQUEST_TIMEOUT_S = 30  # OSSInsight queries usually return in <2s; wide margin
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0

# Window for "_5y" metrics — measured in calendar months ending at run time.
WINDOW_MONTHS = 60

CSV_FIELDS = [
    "repo",
    "repo_id",
    "pr_avg_merge_time_days",
    "issue_avg_close_time_days",
    "prs_opened_5y",
    "prs_merged_5y",
    "prs_closed_5y",
    "active_contributors_5y",
    "commits_5y",
    "unique_committers_5y",
    "forks_growth_5y",
    "stars_growth_5y",
    "last_event_at",
    "top_contributor_login",
    "top_contributor_pr_count",
    "fetched_at",
]


# ── HTTP helpers ────────────────────────────────────────────────────────────


async def ossinsight_get(
    session: aiohttp.ClientSession, path: str, params: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """GET ``OSSINSIGHT_API + path`` with retry on 5xx / timeout. Returns parsed JSON or None."""
    url = f"{OSSINSIGHT_API}{path}"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)) as r:
                if r.status == 200:
                    return await r.json()
                if r.status in (429, 503, 504):
                    # Rate-limit / overload — back off and retry
                    delay = RETRY_BACKOFF_S * attempt + random.uniform(0, 0.5)
                    log.warning("OSSInsight %s -> %d, retry %d/%d after %.1fs",
                                path, r.status, attempt, RETRY_ATTEMPTS, delay)
                    await asyncio.sleep(delay)
                    continue
                # 404 / other — log and give up
                body = (await r.text())[:200]
                log.warning("OSSInsight %s -> %d: %s", path, r.status, body)
                return None
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            delay = RETRY_BACKOFF_S * attempt + random.uniform(0, 0.5)
            log.warning("OSSInsight %s exception: %s — retry %d/%d after %.1fs",
                        path, exc, attempt, RETRY_ATTEMPTS, delay)
            await asyncio.sleep(delay)
    log.error("OSSInsight %s failed after %d attempts", path, RETRY_ATTEMPTS)
    return None


async def resolve_repo_id(session: aiohttp.ClientSession, owner_repo: str) -> str | None:
    """Resolve owner/repo to numeric GitHub repo_id via OSSInsight's GH proxy."""
    owner, repo = owner_repo.split("/", 1)
    payload = await ossinsight_get(session, f"/gh/repo/{owner}/{repo}")
    if not payload:
        return None
    return str(payload.get("data", {}).get("id") or "") or None


async def run_query(
    session: aiohttp.ClientSession, query: str, repo_id: str,
    extra_params: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Run a named OSSInsight query for a repo_id. Returns the full JSON response."""
    params = {"repoId": repo_id}
    if extra_params:
        params.update(extra_params)
    return await ossinsight_get(session, f"/q/{query}", params)


# ── 5y window aggregation ──────────────────────────────────────────────────


def _window_cutoff() -> dt.date:
    today = dt.date.today()
    # 60 calendar months back. Use first-of-month for clean filtering.
    year = today.year - (WINDOW_MONTHS // 12)
    month = today.month
    return dt.date(year, month, 1)


def _parse_month(s: Any) -> dt.date | None:
    """Parse ``YYYY-MM-01`` from OSSInsight payloads (str or datetime-like)."""
    if not s:
        return None
    s = str(s)
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def sum_5y(rows: list[dict[str, Any]], col: str) -> int:
    """Sum a numeric column from a per-month series, restricted to the 5y window."""
    cutoff = _window_cutoff()
    total = 0
    for row in rows:
        m = _parse_month(row.get("event_month"))
        if m and m >= cutoff:
            try:
                total += int(row.get(col) or 0)
            except (TypeError, ValueError):
                pass
    return total


def stars_growth_5y(rows: list[dict[str, Any]]) -> int | None:
    """Difference between latest star total and total at cutoff (cumulative series)."""
    if not rows:
        return None
    cutoff = _window_cutoff()
    sorted_rows = sorted(
        ((_parse_month(r.get("event_month")), r) for r in rows),
        key=lambda x: x[0] or dt.date.min,
    )
    in_window = [(m, r) for m, r in sorted_rows if m and m >= cutoff]
    if not in_window:
        # Repo has no growth in the window — return total since this is the
        # full history.
        try:
            return int(sorted_rows[-1][1].get("total") or 0)
        except (TypeError, ValueError):
            return None
    # Find the last row *before* the window — that's the baseline.
    pre_window = [(m, r) for m, r in sorted_rows if m and m < cutoff]
    if pre_window:
        try:
            baseline = int(pre_window[-1][1].get("total") or 0)
        except (TypeError, ValueError):
            baseline = 0
    else:
        baseline = 0
    try:
        latest = int(in_window[-1][1].get("total") or 0)
    except (TypeError, ValueError):
        return None
    return latest - baseline


def avg_p50_5y(rows: list[dict[str, Any]]) -> float | None:
    """Average of monthly p50 merge / close times (in hours) within the 5y window.

    Returned in **days** (OSSInsight reports in hours).
    """
    cutoff = _window_cutoff()
    values: list[float] = []
    for row in rows:
        m = _parse_month(row.get("event_month"))
        if not m or m < cutoff:
            continue
        p50 = row.get("p50")
        if p50 is None:
            continue
        try:
            values.append(float(p50))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    avg_hours = sum(values) / len(values)
    return round(avg_hours / 24.0, 3)


def latest_event_month(*row_lists: list[dict[str, Any]]) -> str:
    """Return the latest ``event_month`` (or ``current_period_day``) seen across series."""
    latest: dt.date | None = None
    for rows in row_lists:
        for row in rows or []:
            for key in ("event_month", "current_period_day"):
                m = _parse_month(row.get(key))
                if m and (latest is None or m > latest):
                    latest = m
                    break
    return latest.isoformat() if latest else ""


# ── per-repo orchestration ─────────────────────────────────────────────────


async def fetch_repo(
    session: aiohttp.ClientSession, repo: str, repo_id: str | None,
) -> dict[str, str]:
    """Fire all OSSInsight queries for one repo, aggregate, return one CSV row.

    On total failure (no repo_id, or all queries fail) returns a row with
    just ``repo`` populated so the caller can record the gap.
    """
    row = {f: "" for f in CSV_FIELDS}
    row["repo"] = repo
    row["fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # Resolve repo_id if we don't have one
    if not repo_id:
        repo_id = await resolve_repo_id(session, repo)
    if not repo_id:
        log.warning("repo_id unresolved for %s", repo)
        return row
    row["repo_id"] = str(repo_id)

    # Fire all queries concurrently — they're independent.
    # `analyze-repo-top-contributors` defaults to limit=5, so we override
    # to get the full contributor list (used for active_contributors_5y as
    # an upper-bound proxy — true 5y is unavailable, see notes below).
    queries: list[tuple[str, dict[str, str] | None]] = [
        ("analyze-repo-overview", None),
        ("analyze-repo-pr-overview", None),
        ("analyze-repo-issue-overview", None),
        ("analyze-pushes-and-commits-per-month", None),
        ("analyze-stars-history", None),
        ("analyze-pull-request-open-to-merged", None),
        ("analyze-issue-open-to-closed", None),
        ("analyze-pull-requests-size-per-month", None),
        ("analyze-issue-opened-and-closed", None),
        ("analyze-repo-top-contributors", {"limit": "999999"}),
        ("analyze-recent-pull-requests", None),
        ("analyze-recent-contributors", None),
    ]
    results = await asyncio.gather(*(run_query(session, q, repo_id, p) for q, p in queries))
    payload = dict(zip([q for q, _ in queries], results))

    # Cache raw payloads to disk for inspection / re-aggregation later
    cache_path = RAW_CACHE_DIR / f"{repo.replace('/', '_')}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(
        {q: r for q, r in payload.items() if r is not None}, indent=2,
    ))

    # ── pull rows out of each response ──────────────────────────────────
    def _data(q: str) -> list[dict[str, Any]]:
        p = payload.get(q) or {}
        return p.get("data") or []

    pushes = _data("analyze-pushes-and-commits-per-month")
    stars = _data("analyze-stars-history")
    pr_merge = _data("analyze-pull-request-open-to-merged")
    issue_close = _data("analyze-issue-open-to-closed")
    pr_per_month = _data("analyze-pull-requests-size-per-month")
    issue_per_month = _data("analyze-issue-opened-and-closed")
    top_contribs = _data("analyze-repo-top-contributors")
    recent_prs = _data("analyze-recent-pull-requests")

    # ── 5y windowed aggregations ────────────────────────────────────────
    row["commits_5y"] = str(sum_5y(pushes, "commits"))

    sg = stars_growth_5y(stars)
    row["stars_growth_5y"] = str(sg) if sg is not None else ""

    pr_t = avg_p50_5y(pr_merge)
    row["pr_avg_merge_time_days"] = str(pr_t) if pr_t is not None else ""

    issue_t = avg_p50_5y(issue_close)
    row["issue_avg_close_time_days"] = str(issue_t) if issue_t is not None else ""

    # PR throughput in the 5y window
    row["prs_opened_5y"] = str(sum_5y(pr_per_month, "all_size"))
    # `analyze-pull-request-open-to-merged` only has rows for months where
    # PRs were merged — its row count per window is a lower-bound proxy
    # for merged-PR activity. Sum is not directly meaningful (rows are
    # percentile snapshots, not counts) so we leave the strict count
    # unavailable and use opened→closed events as a stand-in via
    # `analyze-issue-opened-and-closed`-style data — but that's issues,
    # not PRs. Without a public query that gives monthly merged-PR counts,
    # we leave these fields empty rather than fake data.
    row["prs_merged_5y"] = ""
    row["prs_closed_5y"] = ""

    # Top contributor and a contributor-pool proxy
    if top_contribs:
        row["top_contributor_login"] = top_contribs[0].get("actor_login", "") or ""
        try:
            row["top_contributor_pr_count"] = str(int(top_contribs[0].get("events") or 0))
        except (TypeError, ValueError):
            pass
        # `analyze-repo-top-contributors` returns ALL lifetime contributors
        # (with limit=9999). The result is lifetime, not 5y — but it's the
        # only contributor count available without a custom SQL endpoint.
        # Marked _5y in the schema for forward-compat; today it's a lifetime
        # upper bound.
        row["active_contributors_5y"] = str(len(top_contribs))

    # Bus-factor-style unique committers in the 5y window — not available
    # from any public OSSInsight named query. Would need a custom SQL on
    # the playground (rate-limited) or BigQuery against githubarchive.
    # See the docstring "Gaps" note. Leaving empty rather than faking.
    row["unique_committers_5y"] = ""

    # Forks growth — github_events doesn't expose a cumulative ForkEvent
    # series. /gh/repo proxy gives current forks_count but no history.
    row["forks_growth_5y"] = ""

    row["last_event_at"] = latest_event_month(pushes, stars, recent_prs, issue_per_month)

    if not (row["commits_5y"] or row["stars_growth_5y"] or row["pr_avg_merge_time_days"]):
        log.warning("All key metrics empty for %s (id=%s) — repo may have no GH-archive activity", repo, repo_id)

    return row


# ── batch runner ───────────────────────────────────────────────────────────


def _is_fresh(row: dict[str, str], ttl_days: int) -> bool:
    """True if ``fetched_at`` < ttl_days old AND the row has at least one populated metric."""
    ts = (row.get("fetched_at") or "").strip()
    if not ts:
        return False
    try:
        d = dt.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - d
    if age.days >= ttl_days:
        return False
    # Don't treat empty rows as fresh — let them be retried
    return any((row.get(k) or "").strip() for k in ("commits_5y", "stars_growth_5y", "pr_avg_merge_time_days"))


def load_existing(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {row["repo"]: row for row in csv.DictReader(f)}


def write_csv(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])
    tmp.replace(path)


async def fetch_all(
    repos: list[tuple[str, str]],
    output_path: Path,
    concurrency: int,
) -> dict[str, dict[str, str]]:
    """Fetch all repos with bounded concurrency, persisting after each one."""
    existing = load_existing(output_path)
    sem = asyncio.Semaphore(concurrency)
    total = len(repos)
    completed = 0
    started = time.monotonic()

    async with aiohttp.ClientSession(headers={"User-Agent": "endowment.dev-ossinsight-fetcher/0.1"}) as session:
        async def bounded(repo: str, repo_id: str) -> None:
            nonlocal completed
            async with sem:
                row = await fetch_repo(session, repo, repo_id)
            existing[repo] = row
            write_csv(output_path, existing)  # checkpoint
            completed += 1
            progress.advance(task)
            if completed % 5 == 0 or completed == total:
                elapsed = time.monotonic() - started
                rate = completed / elapsed if elapsed > 0 else 0
                console.print(
                    f"[dim]{completed}/{total}  {rate:.2f} repos/s  ({repo})[/dim]"
                )

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Fetching OSSInsight metrics", total=total)
            await asyncio.gather(*(bounded(repo, repo_id) for repo, repo_id in repos))

    return existing


# ── CLI ────────────────────────────────────────────────────────────────────


def select_repos(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Return [(repo_slug, repo_id)] selected for this run.

    --test-set: ignore eligibility list, use the spec's curated test repos.
    Otherwise: load eligible repos, optional random --limit sample with --seed.
    """
    if args.test_set:
        return [
            ("hukkin/tomli", "370616797"),
            ("pallets/flask", "596892"),
            ("pyca/cryptography", "11939484"),
            ("urllib3/urllib3", "2410676"),
            ("apache/airflow", "33884891"),
        ]

    entries = load_eligible_repos()
    pool = [(e.repo, e.repo_id or "") for e in entries]

    if args.limit and args.limit < len(pool):
        rng = random.Random(args.seed)
        pool = rng.sample(pool, args.limit)
        pool.sort()
    return pool


def filter_fresh(
    repos: list[tuple[str, str]], existing: dict[str, dict[str, str]], ttl_days: int,
) -> tuple[list[tuple[str, str]], int]:
    """Drop repos whose existing CSV row is fresh (within ttl_days)."""
    keep: list[tuple[str, str]] = []
    skipped = 0
    for repo, repo_id in repos:
        row = existing.get(repo)
        if row and _is_fresh(row, ttl_days):
            skipped += 1
            continue
        keep.append((repo, repo_id))
    return keep, skipped


def display_results(rows: dict[str, dict[str, str]], repos: list[tuple[str, str]]) -> None:
    """Render a rich table summarising the fetched rows for the requested repos."""
    table = Table(title="OSSInsight per-repo metrics (last 5y where applicable)")
    table.add_column("repo", style="cyan", no_wrap=True)
    table.add_column("commits_5y", justify="right")
    table.add_column("stars_grow", justify="right")
    table.add_column("pr_merge_d", justify="right")
    table.add_column("issue_clos_d", justify="right")
    table.add_column("active_contribs", justify="right")
    table.add_column("top_contrib", style="dim")
    table.add_column("last_event")

    for repo, _ in repos:
        r = rows.get(repo) or {"repo": repo}
        table.add_row(
            r.get("repo", ""),
            r.get("commits_5y", "") or "-",
            r.get("stars_growth_5y", "") or "-",
            r.get("pr_avg_merge_time_days", "") or "-",
            r.get("issue_avg_close_time_days", "") or "-",
            r.get("active_contributors_5y", "") or "-",
            (r.get("top_contributor_login", "") or "-")[:18],
            (r.get("last_event_at", "") or "-")[:10],
        )
    console.print(table)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=5,
                   help="Sample N eligible repos (random, deterministic with --seed). 0 = all.")
    p.add_argument("--seed", type=int, default=42, help="Random seed for --limit sampling")
    p.add_argument("--concurrency", type=int, default=4,
                   help="Max concurrent OSSInsight requests (be polite to free public API)")
    p.add_argument("--ttl-days", type=int, default=30,
                   help="Skip repos already fetched within this many days")
    p.add_argument("--force", action="store_true",
                   help="Refetch all selected repos, ignoring TTL")
    p.add_argument("--test-set", action="store_true",
                   help="Use the spec's curated 5-repo test set (ignores --limit)")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return p


async def main() -> None:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        handlers=[RichHandler(console=console, show_time=False, show_path=False)],
        format="%(message)s",
    )
    # Quiet aiohttp internals at DEBUG mode
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    started = dt.datetime.now()
    repos = select_repos(args)
    existing = load_existing(args.output)

    if not args.force:
        repos, skipped = filter_fresh(repos, existing, args.ttl_days)
    else:
        skipped = 0

    # Banner
    banner = Table(show_header=False, box=None, padding=(0, 1))
    banner.add_row("[bold]OSSInsight per-repo fetcher[/bold]")
    banner.add_row(f"output      [cyan]{args.output}[/cyan]")
    banner.add_row(f"repos       [yellow]{len(repos)}[/yellow] (skipped {skipped} fresh)")
    banner.add_row(f"concurrency [yellow]{args.concurrency}[/yellow]")
    banner.add_row(f"ttl-days    [yellow]{args.ttl_days}[/yellow]"
                   + ("  [red](forced)[/red]" if args.force else ""))
    banner.add_row(f"started     [dim]{started.isoformat(timespec='seconds')}[/dim]")
    console.print(banner)

    if not repos:
        console.print("[green]Nothing to fetch.[/green]")
        return

    rows = await fetch_all(repos, args.output, args.concurrency)

    elapsed = (dt.datetime.now() - started).total_seconds()
    rate = len(repos) / elapsed if elapsed > 0 else 0
    console.print(f"\n[green]Done.[/green] {len(repos)} repos in {elapsed:.1f}s ({rate:.2f} repos/s)")
    console.print(f"[green]→ {args.output}[/green]")

    display_results(rows, repos)


if __name__ == "__main__":
    asyncio.run(main())
