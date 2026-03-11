"""Estimate lines of code per repo using GitHub's languages and repo size endpoints.

Usage:
    python -m src.github.locs curl/curl
    python -m src.github.locs                         # batch: top-repos.csv
    python -m src.github.locs --top 10
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import os
import time

import aiohttp
from dotenv import load_dotenv
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TaskProgressColumn, TimeElapsedColumn,
)

from src.github.api import GITHUB_API
from src.github.display import console, _ETAColumn

log = logging.getLogger(__name__)

REPOS_FILE = "data/github/top-repos.csv"
OUTPUT_FILE = "data/github/locs.csv"
LANG_SIZES_FILE = "data/github/language-loc-sizes.csv"
DEFAULT_BPL = 36


def _load_bytes_per_line(filepath: str = LANG_SIZES_FILE) -> dict[str, int]:
    """Load language bytes-per-line mapping from CSV."""
    result: dict[str, int] = {}
    if not os.path.exists(filepath):
        log.warning("Language sizes file not found: %s, using defaults", filepath)
        return result
    with open(filepath, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result[row["language"].lower()] = int(row["bytes_per_line"])
    return result


BYTES_PER_LINE: dict[str, int] = _load_bytes_per_line()


def _estimate_loc(languages: dict[str, int]) -> int:
    """Estimate total LOC from language byte counts."""
    total = 0
    for lang, byte_count in languages.items():
        bpl = BYTES_PER_LINE.get(lang.lower(), DEFAULT_BPL)
        total += byte_count // bpl
    return total


async def _fetch_repo_languages(
    session: aiohttp.ClientSession,
    repo: str,
    progress: Progress | None = None,
    task_id: int | None = None,
    name_width: int = 30,
) -> dict[str, int] | None:
    """Fetch language byte counts for a repo."""
    url = f"{GITHUB_API}/repos/{repo}/languages"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 403:
                # Rate limited — wait for reset
                reset = resp.headers.get("X-RateLimit-Reset")
                if reset:
                    wait = max(float(reset) - time.time() + 0.5, 1)
                    log.debug("%s: rate limited, waiting %.0fs", repo, wait)
                    if progress and task_id is not None:
                        reset_at = datetime.datetime.fromtimestamp(float(reset))
                        desc = f"[yellow]rate limit, reset {reset_at:%H:%M:%S}[/yellow]"
                        progress.update(task_id, description=desc)
                    await asyncio.sleep(wait)
                    return await _fetch_repo_languages(
                        session, repo, progress, task_id, name_width,
                    )
            log.debug("%s: languages HTTP %d", repo, resp.status)
            return None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def fetch_locs(
    repos: list[str], repo_sizes: dict[str, int] | None = None,
    token: str | None = None, output: str = OUTPUT_FILE,
) -> tuple[list[dict], int, int, int]:
    """Fetch language stats for multiple repos concurrently.

    repo_sizes: pre-loaded {repo: size_kb} from top-repos.csv to avoid extra API calls.
    Flushes results to output CSV every 50 repos.
    Returns (results, total, added, updated).
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    sizes = repo_sizes or {}
    timeout = aiohttp.ClientTimeout(total=30)
    sem = asyncio.Semaphore(20)
    results: list[dict] = []
    FLUSH_EVERY = 50

    name_width = min(max((len(r) for r in repos), default=20), 30)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", justify="left"),
        BarColumn(bar_width=14),
        TaskProgressColumn(),
        TextColumn("[dim]{task.completed}/{task.total}[/]"),
        _ETAColumn(),
        console=console,
    )

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        with progress:
            pad = " " * name_width
            task = progress.add_task(pad, total=len(repos))
            pending_flush: list[dict] = []

            async def _process(repo: str) -> dict | None:
                async with sem:
                    langs = await _fetch_repo_languages(
                        session, repo, progress, task, name_width,
                    )
                    desc = repo[:name_width].ljust(name_width)
                    progress.update(task, advance=1, description=desc)
                    if langs is None:
                        return None
                    langs_lower = {k.lower(): v for k, v in langs.items()}
                    code_bytes = sum(langs_lower.values())
                    return {
                        "repo": repo,
                        "languages": langs_lower,
                        "code_size": round(code_bytes / 1024),
                        "total_size": sizes.get(repo, 0),
                        "est_locs": _estimate_loc(langs),
                        "fetched_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }

            total_added = total_updated = 0

            tasks = [_process(r) for r in repos]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                if result:
                    results.append(result)
                    pending_flush.append(result)
                    if len(pending_flush) >= FLUSH_EVERY:
                        total, added, updated = _upsert_csv(output, pending_flush)
                        total_added += added
                        total_updated += updated
                        pending_flush.clear()

            if pending_flush:
                total, added, updated = _upsert_csv(output, pending_flush)
                total_added += added
                total_updated += updated
                pending_flush.clear()

    # total from last _upsert_csv call reflects the full file
    return results, total, total_added, total_updated


def _upsert_csv(filepath: str, rows: list[dict]) -> tuple[int, int, int]:
    """Upsert LOC rows into CSV, keyed by repo. Returns (total, added, updated)."""
    # Collect all language columns across new + existing
    all_langs: set[str] = set()
    for row in rows:
        all_langs.update(row["languages"].keys())

    # Read existing
    existing: dict[str, dict[str, str]] = {}
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                # Extract language columns (everything between repo and code_size)
                for fn in reader.fieldnames:
                    if fn not in ("repo", "code_size", "total_size", "est_locs", "fetched_at"):
                        all_langs.add(fn)
            for row in reader:
                existing[row["repo"]] = row

    added = updated = 0
    for row in rows:
        flat: dict[str, str] = {"repo": row["repo"]}
        for lang in all_langs:
            kb = row["languages"].get(lang, 0)
            flat[lang] = str(round(kb / 1024)) if kb else ""
        flat["code_size"] = str(row["code_size"])
        flat["total_size"] = str(row["total_size"])
        flat["est_locs"] = str(row["est_locs"])
        flat["fetched_at"] = row.get("fetched_at", "")

        if row["repo"] in existing:
            updated += 1
        else:
            added += 1
        existing[row["repo"]] = flat

    # Sort language columns by frequency (most repos using it first)
    lang_freq: dict[str, int] = {}
    for row in existing.values():
        for lang in all_langs:
            if row.get(lang, ""):
                lang_freq[lang] = lang_freq.get(lang, 0) + 1
    sorted_langs = sorted(all_langs, key=lambda l: -lang_freq.get(l, 0))

    fieldnames = ["repo"] + sorted_langs + ["code_size", "total_size", "est_locs", "fetched_at"]
    sorted_rows = sorted(existing.values(), key=lambda r: -int(r.get("est_locs", "0") or "0"))

    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted_rows)

    total = len(existing)
    return total, added, updated


DEFAULT_TTL_DAYS = 30


def _load_repos(filepath: str, top: int | None = None) -> tuple[list[str], dict[str, int]]:
    """Load repo slugs and sizes from CSV. Returns (repos, {repo: size_kb})."""
    repos = []
    sizes: dict[str, int] = {}
    with open(filepath, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repos.append(row["repo"])
            if "size" in row and row["size"]:
                sizes[row["repo"]] = int(row["size"])
    if top:
        repos = repos[:top]
    return repos, sizes


def _filter_by_ttl(repos: list[str], output: str, ttl_days: int) -> tuple[list[str], int]:
    """Filter out repos fetched within TTL. Returns (repos_to_fetch, skipped)."""
    if not os.path.exists(output) or ttl_days <= 0:
        return repos, 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    fresh: set[str] = set()
    with open(output, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row.get("fetched_at", "")
            if ts:
                try:
                    fetched = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if fetched >= cutoff:
                        fresh.add(row["repo"])
                except ValueError:
                    pass
    filtered = [r for r in repos if r not in fresh]
    return filtered, len(repos) - len(filtered)


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate LOC for GitHub repos")
    parser.add_argument("repo", nargs="?", help="GitHub repo (owner/repo)")
    parser.add_argument("--input", default=REPOS_FILE,
                        help=f"CSV with repos to process (default: {REPOS_FILE})")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"Output CSV (default: {OUTPUT_FILE})")
    parser.add_argument("--top", type=int, help="Only process first N repos")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_DAYS,
                        help=f"Skip repos fetched within N days (default: {DEFAULT_TTL_DAYS}, 0 to force refresh)")
    parser.add_argument("--token", help="GitHub personal access token")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    load_dotenv()
    token = args.token or os.environ.get("GITHUB_TOKEN")

    if args.repo:
        repos = [args.repo]
        repo_sizes: dict[str, int] = {}
        skipped = 0
    else:
        repos, repo_sizes = _load_repos(args.input, top=args.top)
        repos, skipped = _filter_by_ttl(repos, args.output, args.ttl)

    if not repos:
        console.print(f"[dim]All repos fresh (TTL {args.ttl}d), nothing to fetch[/dim]")
        return

    t_start = time.monotonic()
    results, total, added, updated = asyncio.run(
        fetch_locs(repos, repo_sizes=repo_sizes, token=token, output=args.output)
    )
    elapsed = time.monotonic() - t_start

    console.print()
    from rich.table import Table
    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim")
    summary.add_column()
    repos_label = f"[bold]{len(results):,}[/bold] of {len(repos):,}"
    if skipped:
        repos_label += f" [dim]({skipped:,} fresh, skipped)[/dim]"
    summary.add_row("Repos", repos_label)
    summary.add_row("Time", f"{elapsed:.1f}s")
    summary.add_row("Output", f"{args.output} ({total:,} total, "
                     f"[green]{added:,} added[/], [yellow]{updated:,} updated[/])")
    console.print(summary)

    if results and args.repo:
        # Single repo: language breakdown
        r = results[0]
        lang_bytes = r["languages"]
        total_code_bytes = sum(lang_bytes.values())

        tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
        tbl.add_column("Language")
        tbl.add_column("MB", justify="right")
        tbl.add_column("%", justify="right")
        tbl.add_column("kLOC", justify="right")

        for lang, b in sorted(lang_bytes.items(), key=lambda x: -x[1]):
            mb = b / 1024 / 1024
            bpl = BYTES_PER_LINE.get(lang, DEFAULT_BPL)
            kloc = b / bpl / 1000
            pct = 100 * b / total_code_bytes if total_code_bytes else 0
            if pct < 0.1:
                continue
            mb_str = f"{mb:,.0f}" if mb >= 1 else f"{mb:,.1f}"
            tbl.add_row(lang, mb_str, f"{pct:.1f}%", f"{kloc:,.0f}")

        tbl.add_section()
        total_mb = total_code_bytes / 1024 / 1024
        total_kloc = r["est_locs"] / 1000
        total_mb_str = f"{total_mb:,.0f}" if total_mb >= 1 else f"{total_mb:,.1f}"
        tbl.add_row("[bold]Total[/bold]", f"[bold]{total_mb_str}[/bold]", "",
                     f"[bold]{total_kloc:,.0f}[/bold]")

        console.print()
        console.print(tbl)

    elif results:
        # Batch: repo summary table
        by_loc = sorted(results, key=lambda r: r["est_locs"], reverse=True)
        show = by_loc[:20]

        tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
        tbl.add_column("Repo")
        tbl.add_column("Top Lang", style="dim")
        tbl.add_column("Code (MB)", justify="right")
        tbl.add_column("kLOC", justify="right")
        tbl.add_column("B/LOC", justify="right")

        for r in show:
            top_lang = max(r["languages"], key=r["languages"].get) if r["languages"] else ""
            mb = r["code_size"] / 1024
            kloc = r["est_locs"] / 1000
            bpl = round(r["code_size"] * 1024 / r["est_locs"]) if r["est_locs"] else 0
            mb_str = f"{mb:,.0f}" if mb >= 1 else f"{mb:,.1f}"
            tbl.add_row(
                r["repo"], top_lang,
                mb_str, f"{kloc:,.0f}", f"{bpl}",
            )

        if len(by_loc) > len(show):
            rest_mb = sum(r["code_size"] for r in by_loc[len(show):]) / 1024
            rest_kloc = sum(r["est_locs"] for r in by_loc[len(show):]) / 1000
            rest_bytes = sum(r["code_size"] * 1024 for r in by_loc[len(show):])
            rest_locs = sum(r["est_locs"] for r in by_loc[len(show):])
            rest_bpl = round(rest_bytes / rest_locs) if rest_locs else 0
            rest_mb_str = f"{rest_mb:,.0f}" if rest_mb >= 1 else f"{rest_mb:,.1f}"
            tbl.add_row(
                f"[dim]... {len(by_loc) - len(show)} more[/dim]", "",
                f"[dim]{rest_mb_str}[/dim]", f"[dim]{rest_kloc:,.0f}[/dim]",
                f"[dim]{rest_bpl}[/dim]",
            )

        tbl.add_section()
        total_mb = sum(r["code_size"] for r in results) / 1024
        total_kloc = sum(r["est_locs"] for r in results) / 1000
        total_bytes = sum(r["code_size"] * 1024 for r in results)
        total_locs = sum(r["est_locs"] for r in results)
        total_bpl = round(total_bytes / total_locs) if total_locs else 0
        total_mb_str = f"{total_mb:,.0f}" if total_mb >= 1 else f"{total_mb:,.1f}"
        tbl.add_row("[bold]Total[/bold]", "",
                     f"[bold]{total_mb_str}[/bold]", f"[bold]{total_kloc:,.0f}[/bold]",
                     f"[bold]{total_bpl}[/bold]")

        console.print()
        console.print(tbl)

    console.print()


if __name__ == "__main__":
    main()
