#!/usr/bin/env python3
"""Git-based contributor metrics — windowed bus factor, HHI, and active
contributors (AC) from a local clone.

`AC` = the count of distinct non-bot people who authored a commit within the
window (2021-2025 by default). It is the windowed denominator the risk
pipeline's workload class wants, and the figure GitHub's `/contributors` API
cannot produce — its `/stats/contributors` (per-week) endpoint 202-loops for
most repos, so the API path only ever yields a lifetime count.

An alternative to the GitHub `/contributors` API path
(`src/github/fetch_contributors_metrics.py`). It walks `git log` on a bare
treeless clone — `bare_treeless_clone` from `src/git/clone.py`, commit graph
only, the lightest clone that supports history walking.

Pipeline per repo:

    bare_treeless_clone      commit graph only — no trees, no blobs
      ↓
    git log --no-merges      author name/email/date, mailmap-resolved (%aN/%aE)
      ↓
    merge_identities()       union-find over email + full-name — one person
                             commits under many name/email pairs
      ↓
    drop bots                is_bot() on derived login + "[bot]" suffix
      ↓
    _compute_bus_factor()    shared with the API path — BF + HHI

Author merging honours the repo's own `.mailmap` first (git applies it to
`%aN`/`%aE`), then a union-find merges identities that share a normalised
email or a full name. It still will not match GitHub's `/contributors` merge
exactly — GitHub merges by *account*, resolving commit emails to GitHub users
server-side, which cannot be reproduced locally.

Modes:
    collect (default)  batch every A/B-class repo → data/git/contributors.csv
                       (resumable: re-running skips status=ok rows)
    --compare N        comparison report on N random already-collected repos
    --inspect REPO     dump one repo's merged contributor list (verify merges)

Usage:
    uv run python -m src.git.contributors                       # collect all
    uv run python -m src.git.contributors --limit 20            # 20 random
    uv run python -m src.git.contributors --concurrency 8 --force
    uv run python -m src.git.contributors --compare 20
    uv run python -m src.git.contributors --inspect curl/curl
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

from src.git.clone import bare_treeless_clone
from src.git.disk import (
    check_disk_or_exit,
    make_clone_tmpdir,
    print_disk_banner,
    sweep_stale_clone_dirs,
)
from src.github.fetch_contributors_metrics import _compute_bus_factor
from src.github.models import Contributor, is_bot
from src.pipeline.common.repos import load_risk_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CONCENTRATION_FILE = DATA_DIR / "concentration-data.csv"
OUTPUT_FILE = DATA_DIR / "git" / "contributors.csv"

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

# Output schema. Numeric columns are blank for non-ok rows.
FIELDS = [
    "repo", "repo_id", "status",
    "ac_2021_2025", "bf_2021_2025", "hhi_2021_2025",
    "contributors_lifetime", "bf_lifetime", "hhi_lifetime",
    "commits_2021_2025", "commits_lifetime", "bots_lifetime",
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

    Reuses `src.github.models.is_bot` (known-bot set + `[bot]` suffix) on the
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

    uf = _UnionFind(len(keys))
    first_by_email: dict[str, int] = {}
    first_by_name: dict[str, int] = {}
    for i, (name, email) in enumerate(keys):
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
    for i in range(len(keys)):
        groups[uf.find(i)].append(i)

    authors: list[Author] = []
    for members in groups.values():
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

def _blank_row(repo: str, repo_id: str) -> dict:
    """A result row with every field present (numeric fields blank)."""
    row = {f: "" for f in FIELDS}
    row["repo"] = repo
    row["repo_id"] = repo_id
    row["fetched_at"] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat(timespec="seconds")
    return row


def process_repo(repo: str, repo_id: str, since: float, until: float,
                 base_dir: str) -> dict:
    """Clone one repo, compute lifetime + windowed metrics, clean up.

    Always returns a row (never raises) — failures are recorded in `status`
    and `error` so the batch can carry on and the CSV stays a complete
    record of what was attempted. `status` ∈ {ok, clone_failed, timeout,
    no_commits, error}.
    """
    row = _blank_row(repo, repo_id)
    dest = os.path.join(base_dir, repo.replace("/", "_"))
    try:
        clone_s, _size = bare_treeless_clone(repo, dest)
        rows = log_commits(dest)
        if not rows:
            row["status"] = "no_commits"
            row["clone_seconds"] = round(clone_s, 1)
            return row
        life = metrics(rows)
        win = metrics(rows, since=since, until=until)
        row.update({
            "status": "ok",
            "ac_2021_2025": win.active_contributors,
            "bf_2021_2025": win.bus_factor,
            "hhi_2021_2025": round(win.hhi * 10000),
            "contributors_lifetime": life.active_contributors,
            "bf_lifetime": life.bus_factor,
            "hhi_lifetime": round(life.hhi * 10000),
            "commits_2021_2025": win.commits,
            "commits_lifetime": life.commits,
            "bots_lifetime": life.bots,
            "clone_seconds": round(clone_s, 1),
        })
    except subprocess.TimeoutExpired:
        row["status"] = "timeout"
        row["error"] = "clone or git log exceeded timeout"
    except RuntimeError as e:
        msg = str(e)
        row["status"] = "clone_failed" if "clone" in msg.lower() else "error"
        row["error"] = msg[:200]
    except Exception as e:  # noqa: BLE001 — batch must survive any single repo
        row["status"] = "error"
        row["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    return row


# ── CSV I/O ─────────────────────────────────────────────────────────────────

def _load_existing(path: Path) -> dict[str, dict]:
    """Load already-collected rows keyed by repo (for resume)."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out[slug] = {k: row.get(k, "") for k in FIELDS}
    return out


def _write_csv(path: Path, rows_by_repo: dict[str, dict]) -> None:
    """Rewrite the whole CSV, rows sorted by repo (atomic via temp + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows_by_repo):
            w.writerow(rows_by_repo[repo])
    os.replace(tmp, path)


# ── batch run ───────────────────────────────────────────────────────────────

async def run_batch(
    targets: list[tuple[str, str]],   # (repo, repo_id)
    window: tuple[int, int],
    concurrency: int,
    output_path: Path,
    force: bool,
    max_disk_gb: float,
) -> dict[str, dict]:
    """Clone + measure every target concurrently, writing the CSV as it goes.

    Resumable: existing `status=ok` rows are kept and skipped unless `force`.
    Rows with any other status are retried (network blips recover; 404s just
    fail fast again). The CSV is rewritten every `FLUSH_EVERY` completions, so
    a crash loses at most that many repos.
    """
    since, until = _window_bounds(*window)
    rows_by_repo = {} if force else _load_existing(output_path)

    to_fetch = [
        (repo, rid) for repo, rid in targets
        if force or rows_by_repo.get(repo, {}).get("status") != "ok"
    ]
    skipped = len(targets) - len(to_fetch)
    console.print(
        f"[bold]Collecting[/bold] {len(targets)} repo(s) · "
        f"{len(to_fetch)} to fetch · {skipped} already ok · "
        f"concurrency {concurrency}\n"
    )
    if not to_fetch:
        console.print("[dim]Nothing to do — all repos already collected.[/dim]")
        return rows_by_repo

    FLUSH_EVERY = 10
    base_dir = make_clone_tmpdir("contributors")
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_running_loop()
    t_start = time.monotonic()

    async def _one(repo: str, rid: str) -> dict:
        async with sem:
            return await loop.run_in_executor(
                None, process_repo, repo, rid, since, until, base_dir
            )

    tasks = [asyncio.create_task(_one(repo, rid)) for repo, rid in to_fetch]
    completed = 0
    aborted = False
    try:
        for fut in asyncio.as_completed(tasks):
            row = await fut
            rows_by_repo[row["repo"]] = row
            completed += 1
            if completed % FLUSH_EVERY == 0:
                _write_csv(output_path, rows_by_repo)
            if completed % 25 == 0 or completed == len(to_fetch):
                rate = completed / max(time.monotonic() - t_start, 1e-9)
                eta = (len(to_fetch) - completed) / max(rate, 1e-9)
                console.print(
                    f"[dim]{completed}/{len(to_fetch)} · "
                    f"{rate:.1f} repo/s · ETA {eta / 60:.1f} min · "
                    f"last: {row['repo']} ({row['status']})[/dim]"
                )
            if max_disk_gb > 0 and not check_disk_or_exit(max_disk_gb, console=console):
                aborted = True
                break
    finally:
        if aborted:
            for t in tasks:
                t.cancel()
        _write_csv(output_path, rows_by_repo)
        shutil.rmtree(base_dir, ignore_errors=True)

    if aborted:
        console.print("[yellow]Aborted on low disk — partial results saved.[/yellow]")
    return rows_by_repo


def _print_summary(rows_by_repo: dict[str, dict], elapsed: float) -> None:
    """Status breakdown + headline AC/BF stats over the collected rows."""
    status = Counter(r.get("status", "") or "?" for r in rows_by_repo.values())
    table = Table(title="[bold]Collection summary[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Status", style="bold")
    table.add_column("Repos", justify="right")
    total = len(rows_by_repo)
    for label in ("ok", "no_commits", "clone_failed", "timeout", "error"):
        n = status.get(label, 0)
        if n:
            table.add_row(label, f"{n:,}")
    table.add_row("[bold]total[/bold]", f"[bold]{total:,}[/bold]")
    console.print(table)

    ok = [r for r in rows_by_repo.values() if r.get("status") == "ok"]
    if ok:
        def _ints(col: str) -> list[int]:
            # Rows are a mix: freshly-computed rows hold ints, CSV-loaded
            # rows hold strings — coerce through str() to handle both.
            vals = []
            for r in ok:
                v = str(r.get(col) if r.get(col) is not None else "").strip()
                if v:
                    try:
                        vals.append(int(float(v)))
                    except ValueError:
                        pass
            return vals
        ac = _ints("ac_2021_2025")
        if ac:
            ac.sort()
            console.print(
                f"[dim]AC 2021-2025 over {len(ac):,} ok repos — "
                f"min {ac[0]} · median {ac[len(ac) // 2]} · max {ac[-1]:,}[/dim]"
            )
    console.print(f"[dim]elapsed {elapsed / 60:.1f} min[/dim]")


# ── comparison + inspection ─────────────────────────────────────────────────

def _load_github_numbers() -> dict[str, dict[str, str]]:
    """Lifetime BF/HHI/contributor counts from the API path's output.

    Reads `data/concentration-data.csv` (produced by the `/contributors`
    fetcher). `hhi` there is on a 0-10000 scale; `bus_factor` is the lifetime
    bus factor; `active_contributors` is the non-bot contributor count.
    """
    out: dict[str, dict[str, str]] = {}
    if not CONCENTRATION_FILE.exists():
        return out
    with open(CONCENTRATION_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out[slug] = row
    return out


def _compare(n: int, seed: int) -> int:
    """Print a git-vs-GitHub comparison for `n` random collected repos."""
    collected = _load_existing(OUTPUT_FILE)
    if not collected:
        console.print(f"[red]No collected data at {OUTPUT_FILE} — run collection first.[/red]")
        return 1
    gh = _load_github_numbers()
    ok = [r for r in collected.values()
          if r.get("status") == "ok" and r["repo"] in gh]
    if not ok:
        console.print("[red]No collected repos overlap concentration-data.csv.[/red]")
        return 1
    random.seed(seed)
    sample = random.sample(ok, min(n, len(ok)))
    sample.sort(key=lambda r: r["repo"])

    table = Table(
        title=f"[bold]git vs GitHub — {len(sample)} random repos (seed {seed})[/bold]",
        show_header=True, header_style="bold dim", padding=(0, 1),
    )
    table.add_column("Repo")
    table.add_column("BF g/h", justify="right")
    table.add_column("HHI g/h", justify="right")
    table.add_column("Contrib g/h", justify="right")
    table.add_column("AC 21-25", justify="right", style="yellow")

    bf_exact = hhi_close = 0
    for r in sample:
        g = gh[r["repo"]]
        gh_bf = (g.get("bus_factor") or "").strip()
        gh_hhi = (g.get("hhi") or "").strip()
        gh_c = (g.get("active_contributors") or "").strip()
        git_bf = r["bf_lifetime"]
        git_hhi = r["hhi_lifetime"]
        git_c = r["contributors_lifetime"]
        if gh_bf and git_bf and gh_bf == str(git_bf):
            bf_exact += 1
        try:
            if gh_hhi and git_hhi and abs(int(gh_hhi) - int(git_hhi)) <= 500:
                hhi_close += 1
        except ValueError:
            pass
        table.add_row(
            r["repo"],
            f"{git_bf}/{gh_bf or '—'}",
            f"{git_hhi}/{gh_hhi or '—'}",
            f"{git_c}/{gh_c or '—'}",
            str(r["ac_2021_2025"]),
        )
    console.print(table)
    console.print(
        f"[dim]BF exact match: {bf_exact}/{len(sample)} · "
        f"HHI within 500 (0-10000 scale): {hhi_close}/{len(sample)}[/dim]"
    )
    console.print(
        "[dim]Contrib g/h differs by design: git counts every distinct "
        "identity; GitHub's /contributors merges by account and caps "
        "the named list near 500.[/dim]"
    )
    return 0


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
        description="Git-based windowed bus factor / HHI / active contributors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("repos", nargs="*",
                        help="specific owner/name slugs to collect (default: all A/B repos)")
    parser.add_argument("--window", type=int, nargs=2, metavar=("START", "END"),
                        default=list(DEFAULT_WINDOW),
                        help="contribution window years (default: 2021 2025)")
    parser.add_argument("--limit", type=int, default=0,
                        help="collect only N random A/B repos (testing)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="parallel clone+log workers (default: 8)")
    parser.add_argument("--force", action="store_true",
                        help="re-collect every repo, ignoring existing ok rows")
    parser.add_argument("--max-disk-gb", type=float, default=2.0,
                        help="abort if free temp disk drops below this (default: 2.0)")
    parser.add_argument("--seed", type=int, default=42,
                        help="random seed for --limit / --compare sampling")
    parser.add_argument("--compare", type=int, metavar="N", default=0,
                        help="comparison report on N random collected repos, then exit")
    parser.add_argument("--inspect", metavar="REPO", default="",
                        help="dump one repo's merged contributor list, then exit")
    args = parser.parse_args()
    window = (args.window[0], args.window[1])

    # Never let a private/credential-gated clone hang on an interactive prompt.
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

    if args.compare:
        return _compare(args.compare, args.seed)
    if args.inspect:
        return _inspect(args.inspect, window)

    # Build the target list.
    if args.repos:
        targets = [(r.strip().lower(), "") for r in args.repos]
    else:
        entries = load_risk_repos()
        targets = [(e.repo, e.repo_id) for e in entries]
        if args.limit and args.limit < len(targets):
            random.seed(args.seed)
            targets = random.sample(targets, args.limit)

    console.print(
        f"[bold]Git-based contributor collection[/bold] — "
        f"window {window[0]}-{window[1]} · "
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
    )
    sweep_stale_clone_dirs(console=console)
    print_disk_banner(console=console)
    console.print()

    t0 = time.monotonic()
    rows_by_repo = asyncio.run(run_batch(
        targets, window, args.concurrency, OUTPUT_FILE, args.force,
        args.max_disk_gb,
    ))
    console.print()
    _print_summary(rows_by_repo, time.monotonic() - t0)
    console.print(f"[dim]Wrote {OUTPUT_FILE}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
