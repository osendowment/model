#!/usr/bin/env python3
"""Git-based contributor data fetcher — dumps long raw per-author commit data.

Walks `git log` on a bare treeless clone and writes two files:

    data/sources/git/contributor-commits.csv      long raw: one row per
                                           (repo, author, year)
    data/sources/git/contributor-commits.status.csv  per-repo status sidecar

Metric computation (bus factor, HHI, active contributors) has moved to
`build_concentration.py`, which reads the long CSV and aggregates over it.
This matches the repo's scc/lizard pattern: fetchers dump raw long data;
builders compute derived metrics.

Pipeline per repo:

    bare_treeless_clone      commit graph only — no trees, no blobs
      ↓
    git log --no-merges      author name/email/date, mailmap-resolved (%aN/%aE)
      ↓
    bucket by (name, email, year)   Counter → long rows

Author names/emails are the raw mailmap-resolved values from `%aN`/`%aE`.
Identity merging (union-find over shared emails + full names) is intentionally
deferred to the builder so it can apply globally-consistent rules. The helper
`merge_identity_groups` is exported here for the builder and --inspect to use.

Modes:
    collect (default)  batch every A/B-class repo → two CSV files
                       (resumable: re-running skips status=ok rows)
    --inspect REPO     dump one repo's merged contributor list (verify merges)

Usage:
    uv run python -m src.sources.git.contributors                       # collect all
    uv run python -m src.sources.git.contributors --limit 20            # 20 random
    uv run python -m src.sources.git.contributors --concurrency 8 --force
    uv run python -m src.sources.git.contributors --inspect curl/curl
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import load_repo_ids, load_risk_repos
from src.sources.git.clone import bare_treeless_clone
from src.sources.git.disk import (
    check_disk_or_exit,
    make_clone_tmpdir,
    print_disk_banner,
    sweep_stale_clone_dirs,
)
from src.sources.github.fetch_contributors_metrics import _compute_bus_factor
from src.sources.github.models import Contributor, is_bot

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "git" / "contributor-commits.csv"
STATUS_FILE = DATA_DIR / "sources" / "git" / "contributor-commits.status.csv"

# Default contribution window — matches the risk pipeline's 2021-2025 frame.
DEFAULT_WINDOW = (2021, 2025)

# GitHub no-reply emails: "12345+login@users.noreply.github.com" or the older
# "login@users.noreply.github.com". The numeric prefix is the account id —
# stripping it lets a person's numbered and un-numbered no-reply addresses
# merge into one identity.
_GH_NOREPLY = re.compile(
    r"^(?:\d+\+)?(?P<login>[^@]+)@users\.noreply\.github\.com$", re.IGNORECASE
)

# Author emails that mark a commit as bot-authored even when the name and
# login give nothing away. `noreply@github.com` is the author on commits
# made through GitHub's web UI / "web-flow".
_BOT_EMAILS = {
    "noreply@github.com",
    "actions@github.com",
}

# Long raw output: one row per (repo, author_name, author_email, year).
LONG_FIELDS = ["repo", "author_name", "author_email", "year", "commits"]

# Per-repo status sidecar.
STATUS_FIELDS = [
    "repo", "repo_id", "status",
    "distinct_authors", "commits_total",
    "clone_seconds", "error", "fetched_at",
]


# ── identity model ──────────────────────────────────────────────────────────

@dataclass
class Author:
    """One merged person: a display name, a login token, a commit count."""
    name: str
    login: str
    commits: int
    is_bot: bool


@dataclass
class GitMetrics:
    """BF / HHI / contributor-count result for one repo over one time window.

    `active_contributors` is the distinct non-bot people count. Over the
    2021-2025 window it is the **AC** the risk pipeline wants — the windowed
    figure GitHub's `/contributors` API cannot provide.
    """
    bus_factor: int
    hhi: float                  # 0..1 (sum of squared commit shares)
    active_contributors: int    # distinct non-bot people (windowed → AC)
    bots: int                   # distinct bot identities dropped
    commits: int                # non-bot commits counted


class _UnionFind:
    """Minimal union-find for merging author identities."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, i: int) -> int:
        root = i
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[i] != root:  # path compression
            self._parent[i], i = root, self._parent[i]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


# ── git log ─────────────────────────────────────────────────────────────────

def log_commits(clone_dir: str, branch: str = "HEAD",
                timeout: int = 300) -> list[tuple[float, str, str]]:
    """Return (author_unix_time, author_name, author_email) for every non-merge
    commit reachable from `branch`.

    Names/emails are mailmap-resolved: `%aN`/`%aE` apply the repo's own
    `.mailmap`, so maintainer-curated identity merges come for free. Merge
    commits are excluded — they are routing, not authored work, and GitHub's
    contributor graph likewise does not credit them.
    """
    result = subprocess.run(
        ["git", "-C", clone_dir, "log", branch, "--no-merges",
         "--pretty=format:%at%x1f%aN%x1f%aE"],
        capture_output=True, timeout=timeout,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git log failed in {clone_dir}: {err[:200]}")
    # Author names/emails are NOT guaranteed UTF-8 — long-lived repos carry
    # commits with latin-1 (or other) bytes. Decode leniently so one bad
    # byte deep in the history doesn't sink the whole repo.
    stdout = result.stdout.decode("utf-8", "replace")
    rows: list[tuple[float, str, str]] = []
    for line in stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        ts_raw, name, email = parts
        try:
            ts = float(ts_raw)
        except ValueError:
            continue
        rows.append((ts, name.strip(), email.strip()))
    return rows


# ── identity merging ────────────────────────────────────────────────────────

def _canon_email(email: str) -> str:
    """Normalise an email for identity matching.

    Lowercased; a GitHub no-reply address collapses to `<login>@noreply` so
    the numbered and un-numbered forms of one account merge.
    """
    e = email.strip().lower()
    m = _GH_NOREPLY.match(e)
    if m:
        return f"{m.group('login')}@noreply"
    return e


def _derive_login(name: str, email: str) -> str:
    """Pick a stable login-ish token for a person — used for bots + display.

    Preference: GitHub no-reply login → email local-part → lowercased name.
    """
    m = _GH_NOREPLY.match(email.strip().lower())
    if m:
        return m.group("login")
    local = email.strip().lower().split("@", 1)[0]
    return local or name.strip().lower()


def _is_bot_identity(names: list[str], emails: list[str]) -> bool:
    """True if any name/email in a merged identity marks it as a bot.

    Reuses `src.sources.github.models.is_bot` (known-bot set + `[bot]` suffix) on the
    name and on the no-reply login, and checks a small bot-email set.
    """
    for n in names:
        nl = n.strip().lower()
        if nl.endswith("[bot]") or is_bot(nl):
            return True
    for e in emails:
        el = e.strip().lower()
        if el in _BOT_EMAILS:
            return True
        m = _GH_NOREPLY.match(el)
        if m and is_bot(m.group("login")):
            return True
    return False


def merge_identity_groups(pairs: list[tuple[str, str]]) -> list[list[int]]:
    """Union-find merge of (name, email) identity pairs.

    Returns a list of groups; each group is a list of indices into `pairs`.
    Two pairs are unioned when they share a normalised email (`_canon_email`)
    or a full name (lowercased, containing a space). This is the grouping
    `merge_identities` is built on — exposed so callers that need per-group
    metrics other than a flat commit count (e.g. windowed sums) can reuse it.
    """
    if not pairs:
        return []

    uf = _UnionFind(len(pairs))
    first_by_email: dict[str, int] = {}
    first_by_name: dict[str, int] = {}
    for i, (name, email) in enumerate(pairs):
        ce = _canon_email(email)
        if ce:
            if ce in first_by_email:
                uf.union(first_by_email[ce], i)
            else:
                first_by_email[ce] = i
        nn = name.strip().lower()
        if " " in nn:  # only merge on names that look like real full names
            if nn in first_by_name:
                uf.union(first_by_name[nn], i)
            else:
                first_by_name[nn] = i

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(pairs)):
        groups[uf.find(i)].append(i)

    return list(groups.values())


def merge_identities(counts: dict[tuple[str, str], int]) -> list[Author]:
    """Merge (name, email) → commit-count pairs into one record per person.

    Two identities are unioned when they share a normalised email, or share a
    *full* name (one containing a space — single-token handles like "ci" are
    too collision-prone to merge on). Each merged person's display name is
    taken from the identity with the most commits.
    """
    keys = list(counts)
    if not keys:
        return []

    groups = merge_identity_groups(keys)

    authors: list[Author] = []
    for members in groups:
        total = sum(counts[keys[m]] for m in members)
        lead = max(members, key=lambda m: counts[keys[m]])
        lead_name, lead_email = keys[lead]
        names = [keys[m][0] for m in members]
        emails = [keys[m][1] for m in members]
        authors.append(Author(
            name=lead_name,
            login=_derive_login(lead_name, lead_email),
            commits=total,
            is_bot=_is_bot_identity(names, emails),
        ))
    return authors


# ── metrics ─────────────────────────────────────────────────────────────────

def _window_counts(
    rows: list[tuple[float, str, str]],
    since: float | None, until: float | None,
) -> dict[tuple[str, str], int]:
    """Commit count per raw (name, email) within [since, until] by author date."""
    counts: Counter[tuple[str, str]] = Counter()
    for ts, name, email in rows:
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        counts[(name, email)] += 1
    return counts


def merged_authors(
    rows: list[tuple[float, str, str]],
    since: float | None = None, until: float | None = None,
) -> list[Author]:
    """Merged, bot-flagged author list over `rows`, optionally windowed."""
    return merge_identities(_window_counts(rows, since, until))


def metrics(
    rows: list[tuple[float, str, str]],
    since: float | None = None, until: float | None = None,
) -> GitMetrics:
    """Compute BF/HHI over `rows`, optionally windowed to [since, until].

    Identities are merged and bots dropped before BF/HHI; the shared
    `_compute_bus_factor` is called with `include_bots=True` so it does not
    re-filter (we have already removed bots — the API path's login-based
    filter would not recognise git authors anyway).
    """
    authors = merged_authors(rows, since, until)
    humans = [a for a in authors if not a.is_bot]
    bots = [a for a in authors if a.is_bot]
    contributors = [
        Contributor(login=a.login, lines_changed=0, commits=a.commits)
        for a in humans
    ]
    bus_factor, _sorted, hhi = _compute_bus_factor(contributors, include_bots=True)
    return GitMetrics(
        bus_factor=bus_factor, hhi=hhi,
        active_contributors=len(humans), bots=len(bots),
        commits=sum(a.commits for a in humans),
    )


def _window_bounds(start_year: int, end_year: int) -> tuple[float, float]:
    """Unix-time bounds for [Jan 1 start_year, Dec 31 end_year], UTC."""
    since = datetime.datetime(start_year, 1, 1, tzinfo=datetime.timezone.utc)
    until = datetime.datetime(end_year, 12, 31, 23, 59, 59,
                              tzinfo=datetime.timezone.utc)
    return since.timestamp(), until.timestamp()


# ── per-repo collection ─────────────────────────────────────────────────────

def process_repo(repo: str, repo_id: str, base_dir: str,
                 timeout: int = 300) -> tuple[list[dict], dict]:
    """Clone one repo, bucket commits by (author, year), clean up.

    Returns (long_rows, status_row). Always returns without raising — failures
    are recorded in status_row so the batch can carry on.

    long_rows: list of dicts with LONG_FIELDS keys — one per (name, email, year).
    status_row: dict with STATUS_FIELDS keys.
    `status` ∈ {ok, clone_failed, timeout, no_commits, error}.
    `timeout` (seconds) bounds both the clone and the `git log` — raise it for
    kernel-scale mirrors that overrun the 300s default.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    status_row: dict = {f: "" for f in STATUS_FIELDS}
    status_row["repo"] = repo
    status_row["repo_id"] = repo_id
    status_row["fetched_at"] = now

    dest = os.path.join(base_dir, repo.replace("/", "_"))
    long_rows: list[dict] = []
    try:
        clone_s, _size = bare_treeless_clone(repo, dest, timeout=timeout)
        commit_rows = log_commits(dest, timeout=timeout)
        if not commit_rows:
            status_row["status"] = "no_commits"
            status_row["clone_seconds"] = round(clone_s, 1)
            return [], status_row

        # Bucket by (name, email, year).
        bucket: Counter[tuple[str, str, int]] = Counter()
        for ts, name, email in commit_rows:
            year = datetime.datetime.fromtimestamp(
                ts, tz=datetime.timezone.utc
            ).year
            bucket[(name, email, year)] += 1

        for (name, email, year), count in bucket.items():
            long_rows.append({
                "repo": repo,
                "author_name": name,
                "author_email": email,
                "year": year,
                "commits": count,
            })

        distinct_authors = len({(name, email) for name, email, _ in bucket})
        commits_total = sum(bucket.values())

        status_row.update({
            "status": "ok",
            "distinct_authors": distinct_authors,
            "commits_total": commits_total,
            "clone_seconds": round(clone_s, 1),
        })
    except subprocess.TimeoutExpired:
        status_row["status"] = "timeout"
        status_row["error"] = "clone or git log exceeded timeout"
    except RuntimeError as e:
        msg = str(e)
        status_row["status"] = "clone_failed" if "clone" in msg.lower() else "error"
        status_row["error"] = msg[:200]
    except Exception as e:  # noqa: BLE001 — batch must survive any single repo
        status_row["status"] = "error"
        status_row["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        shutil.rmtree(dest, ignore_errors=True)

    if status_row.get("status") != "ok":
        long_rows = []
    return long_rows, status_row


# ── CSV I/O ─────────────────────────────────────────────────────────────────

def _load_status(path: Path) -> dict[str, dict]:
    """Load status sidecar keyed by repo."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out[slug] = {k: row.get(k, "") for k in STATUS_FIELDS}
    return out


def _load_long(path: Path) -> dict[str, list[dict]]:
    """Load long CSV grouped by repo (for resume — keeps rows of non-refetched repos)."""
    out: dict[str, list[dict]] = defaultdict(list)
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out[slug].append({k: row.get(k, "") for k in LONG_FIELDS})
    return out


def _write_long(path: Path, long_by_repo: dict[str, list[dict]]) -> None:
    """Write all long rows sorted by repo (atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LONG_FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(long_by_repo):
            for row in long_by_repo[repo]:
                w.writerow(row)
    os.replace(tmp, path)


def _write_status(path: Path, status_by_repo: dict[str, dict]) -> None:
    """Write status sidecar sorted by repo (atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STATUS_FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(status_by_repo):
            w.writerow(status_by_repo[repo])
    os.replace(tmp, path)


# ── batch run ───────────────────────────────────────────────────────────────

async def run_batch(
    targets: list[tuple[str, str]],   # (repo, repo_id)
    concurrency: int,
    output_path: Path,
    status_path: Path,
    force: bool,
    max_disk_gb: float,
    timeout: int = 300,
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Clone + bucket every target concurrently, writing CSVs as it goes.

    Resumable: existing `status=ok` rows are kept and skipped unless `force`.
    Both files are rewritten every ~25 completions. Returns (long_by_repo,
    status_by_repo) covering all known repos (fetched + previously collected).
    """
    # Load existing data so non-targeted repos survive.
    status_by_repo = _load_status(status_path)
    long_by_repo = _load_long(output_path)

    to_fetch = [
        (repo, rid) for repo, rid in targets
        if force or status_by_repo.get(repo, {}).get("status") != "ok"
    ]
    skipped = len(targets) - len(to_fetch)
    console.print(
        f"[bold]Collecting[/bold] {len(targets)} repo(s) · "
        f"{len(to_fetch)} to fetch · {skipped} already ok · "
        f"concurrency {concurrency}\n"
    )
    if not to_fetch:
        console.print("[dim]Nothing to do — all repos already collected.[/dim]")
        return long_by_repo, status_by_repo

    FLUSH_EVERY = 25
    base_dir = make_clone_tmpdir("contributors")
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_running_loop()
    t_start = time.monotonic()

    async def _one(repo: str, rid: str) -> tuple[str, list[dict], dict]:
        async with sem:
            long_rows, status_row = await loop.run_in_executor(
                None, process_repo, repo, rid, base_dir, timeout
            )
            return repo, long_rows, status_row

    tasks = [asyncio.create_task(_one(repo, rid)) for repo, rid in to_fetch]
    completed = 0
    aborted = False
    try:
        for fut in asyncio.as_completed(tasks):
            repo, long_rows, status_row = await fut
            long_by_repo[repo] = long_rows
            status_by_repo[repo] = status_row
            completed += 1
            if completed % FLUSH_EVERY == 0:
                _write_long(output_path, long_by_repo)
                _write_status(status_path, status_by_repo)
            if completed % 25 == 0 or completed == len(to_fetch):
                rate = completed / max(time.monotonic() - t_start, 1e-9)
                eta = (len(to_fetch) - completed) / max(rate, 1e-9)
                console.print(
                    f"[dim]{completed}/{len(to_fetch)} · "
                    f"{rate:.1f} repo/s · ETA {eta / 60:.1f} min · "
                    f"last: {repo} ({status_row['status']})[/dim]"
                )
            if max_disk_gb > 0 and not check_disk_or_exit(max_disk_gb, console=console):
                aborted = True
                break
    finally:
        if aborted:
            for t in tasks:
                t.cancel()
        _write_long(output_path, long_by_repo)
        _write_status(status_path, status_by_repo)
        shutil.rmtree(base_dir, ignore_errors=True)

    if aborted:
        console.print("[yellow]Aborted on low disk — partial results saved.[/yellow]")
    return long_by_repo, status_by_repo


def _print_summary(
    long_by_repo: dict[str, list[dict]],
    status_by_repo: dict[str, dict],
    elapsed: float,
) -> None:
    """Status breakdown + aggregate stats over ok repos."""
    status = Counter(r.get("status", "") or "?" for r in status_by_repo.values())
    table = Table(title="[bold]Collection summary[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Status", style="bold")
    table.add_column("Repos", justify="right")
    total = len(status_by_repo)
    for label in ("ok", "no_commits", "clone_failed", "timeout", "error"):
        n = status.get(label, 0)
        if n:
            table.add_row(label, f"{n:,}")
    table.add_row("[bold]total[/bold]", f"[bold]{total:,}[/bold]")
    console.print(table)

    ok_repos = [r for r in status_by_repo.values() if r.get("status") == "ok"]
    if ok_repos:
        def _int(r: dict, col: str) -> int:
            v = str(r.get(col) or "").strip()
            try:
                return int(float(v))
            except ValueError:
                return 0

        total_authors = sum(_int(r, "distinct_authors") for r in ok_repos)
        total_commits = sum(_int(r, "commits_total") for r in ok_repos)
        console.print(
            f"[dim]ok repos: {len(ok_repos):,} · "
            f"total distinct authors: {total_authors:,} · "
            f"total commits: {total_commits:,}[/dim]"
        )
    console.print(f"[dim]elapsed {elapsed / 60:.1f} min[/dim]")


# ── inspection ──────────────────────────────────────────────────────────────

def _inspect(repo: str, window: tuple[int, int]) -> int:
    """Clone one repo and dump its merged contributor list — to verify merges."""
    since, until = _window_bounds(*window)
    base_dir = make_clone_tmpdir("contributors")
    dest = os.path.join(base_dir, repo.replace("/", "_"))
    try:
        clone_s, _ = bare_treeless_clone(repo, dest)
        rows = log_commits(dest)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]{repo}: {e}[/red]")
        shutil.rmtree(base_dir, ignore_errors=True)
        return 1
    finally:
        # keep dest until after log_commits; cleaned below
        pass

    life = sorted(merged_authors(rows), key=lambda a: a.commits, reverse=True)
    win = sorted(merged_authors(rows, since, until),
                 key=lambda a: a.commits, reverse=True)
    shutil.rmtree(base_dir, ignore_errors=True)

    table = Table(title=f"[bold]{repo}[/bold] — top merged contributors (lifetime)",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("#", style="dim", justify="right")
    table.add_column("Name")
    table.add_column("login")
    table.add_column("Commits", justify="right")
    table.add_column("bot", justify="center")
    for i, a in enumerate(life[:25], 1):
        table.add_row(str(i), a.name, a.login, f"{a.commits:,}",
                      "[red]yes[/red]" if a.is_bot else "")
    console.print(table)
    life_h = [a for a in life if not a.is_bot]
    win_h = [a for a in win if not a.is_bot]
    console.print(
        f"[dim]clone {clone_s:.1f}s · {len(rows):,} non-merge commits · "
        f"lifetime: {len(life_h):,} humans + {len(life) - len(life_h)} bots · "
        f"{window[0]}-{window[1]} AC: {len(win_h):,}[/dim]"
    )
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Git-based contributor data fetcher — dumps long raw per-author commits.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("repos", nargs="*",
                        help="specific owner/name slugs to collect (default: all A/B repos)")
    parser.add_argument("--window", type=int, nargs=2, metavar=("START", "END"),
                        default=list(DEFAULT_WINDOW),
                        help="window years for --inspect only (default: 2021 2025)")
    parser.add_argument("--limit", type=int, default=0,
                        help="collect only N random A/B repos (testing)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="parallel clone+log workers (default: 8)")
    parser.add_argument("--force", action="store_true",
                        help="re-collect every repo, ignoring existing ok rows")
    parser.add_argument("--max-disk-gb", type=float, default=2.0,
                        help="abort if free temp disk drops below this (default: 2.0)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="per-repo clone + git-log timeout in seconds "
                             "(default: 300; raise for kernel-scale mirrors)")
    parser.add_argument("--seed", type=int, default=42,
                        help="random seed for --limit sampling")
    parser.add_argument("--inspect", metavar="REPO", default="",
                        help="dump one repo's merged contributor list, then exit")
    args = parser.parse_args()
    window = (args.window[0], args.window[1])

    # Never let a private/credential-gated clone hang on an interactive prompt.
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

    if args.inspect:
        return _inspect(args.inspect, window)

    # Build the target list.
    if args.repos:
        ids = load_repo_ids()
        targets = [(s, ids.get(s, ""))
                   for s in (r.strip().lower() for r in args.repos)]
    else:
        entries = load_risk_repos()
        targets = [(e.repo, e.repo_id) for e in entries]
        if args.limit and args.limit < len(targets):
            random.seed(args.seed)
            targets = random.sample(targets, args.limit)

    console.print(
        f"[bold]Git-based contributor data fetcher[/bold] — "
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
    )
    sweep_stale_clone_dirs(console=console)
    print_disk_banner(console=console)
    console.print()

    t0 = time.monotonic()
    long_by_repo, status_by_repo = asyncio.run(run_batch(
        targets, args.concurrency, OUTPUT_FILE, STATUS_FILE,
        args.force, args.max_disk_gb, args.timeout,
    ))
    console.print()
    _print_summary(long_by_repo, status_by_repo, time.monotonic() - t0)
    console.print(f"[dim]Wrote {OUTPUT_FILE}[/dim]")
    console.print(f"[dim]Wrote {STATUS_FILE}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
