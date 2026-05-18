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
blobless clone — the same clone family `fetch_churn` uses (`src/git/clone.py`)
— so contributions can be windowed to an arbitrary date range. GitHub's
`/stats/contributors` endpoint cannot do this reliably (it returns HTTP 202
"computing" indefinitely for ~90% of repos), which is why the API path only
ever produces a lifetime aggregate.

Pipeline per repo:

    bare_blobless_clone      full commit graph, no blobs (fast, history only)
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
email or a full name. This still will not match GitHub's `/contributors`
merge exactly: GitHub merges by *account*, resolving commit emails to GitHub
users server-side — a mapping we cannot reproduce locally. The comparison
table surfaces that gap.

Usage:
    uv run python -m src.git.contributors                    # curl + openssl
    uv run python -m src.git.contributors curl/curl rust-lang/rust
    uv run python -m src.git.contributors --window 2021 2025 curl/curl
"""
from __future__ import annotations

import argparse
import csv
import datetime
import os
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

from src.git.clone import bare_blobless_clone
from src.git.disk import make_clone_tmpdir, print_disk_banner, sweep_stale_clone_dirs
from src.github.fetch_contributors_metrics import _compute_bus_factor
from src.github.models import Contributor, is_bot

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CONCENTRATION_FILE = DATA_DIR / "concentration-data.csv"

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
    authors = merge_identities(_window_counts(rows, since, until))
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


# ── GitHub comparison ───────────────────────────────────────────────────────

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


# ── CLI ─────────────────────────────────────────────────────────────────────

def _report(repo: str, life: GitMetrics, win: GitMetrics,
            window: tuple[int, int], gh: dict[str, str] | None,
            clone_s: float, log_n: int) -> None:
    """Print a per-repo comparison table: git lifetime / GitHub / git windowed."""
    table = Table(title=f"[bold]{repo}[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Source", style="bold")
    table.add_column("BF", justify="right")
    table.add_column("HHI", justify="right")
    table.add_column("Contrib / AC", justify="right")

    table.add_row("git · lifetime",
                  str(life.bus_factor), f"{round(life.hhi * 10000):,}",
                  f"{life.active_contributors:,}")
    if gh:
        gh_bf = (gh.get("bus_factor") or "—").strip() or "—"
        gh_hhi = (gh.get("hhi") or "—").strip() or "—"
        gh_c = (gh.get("active_contributors") or "—").strip() or "—"
        try:
            gh_hhi = f"{int(gh_hhi):,}"
        except ValueError:
            pass
        try:
            gh_c = f"{int(gh_c):,}"
        except ValueError:
            pass
        table.add_row("GitHub API · lifetime", gh_bf, gh_hhi, gh_c,
                      style="cyan")
    else:
        table.add_row("GitHub API · lifetime", "[dim]not in concentration-data[/dim]",
                      "", "", style="cyan")
    table.add_row(f"git · {window[0]}-{window[1]}  (AC window)",
                  str(win.bus_factor), f"{round(win.hhi * 10000):,}",
                  f"{win.active_contributors:,}", style="yellow")

    console.print(table)
    console.print(
        f"[bold green]→ AC (active contributors {window[0]}-{window[1]}): "
        f"{win.active_contributors:,}[/bold green]   "
        f"[dim]windowed BF {win.bus_factor}, HHI {round(win.hhi * 10000):,}[/dim]"
    )
    console.print(
        f"[dim]clone {clone_s:.1f}s · {log_n:,} non-merge commits · "
        f"bots dropped: {life.bots} (lifetime)[/dim]\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Git-based windowed bus factor + HHI from a local clone.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("repos", nargs="*", default=["curl/curl", "openssl/openssl"],
                        help="owner/name slugs (default: curl/curl openssl/openssl)")
    parser.add_argument("--window", type=int, nargs=2, metavar=("START", "END"),
                        default=list(DEFAULT_WINDOW),
                        help="contribution window years (default: 2021 2025)")
    args = parser.parse_args()
    repos = args.repos or ["curl/curl", "openssl/openssl"]
    win_years = (args.window[0], args.window[1])

    console.print(
        f"[bold]Git-based contributor metrics[/bold] — {len(repos)} repo(s), "
        f"window {win_years[0]}-{win_years[1]} · "
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
    )
    sweep_stale_clone_dirs(console=console)
    print_disk_banner(console=console)
    console.print()

    since, until = _window_bounds(*win_years)
    gh_numbers = _load_github_numbers()

    exit_code = 0
    for repo in repos:
        base = make_clone_tmpdir("contributors")
        dest = os.path.join(base, repo.replace("/", "_"))
        try:
            t0 = time.monotonic()
            bare_blobless_clone(repo, dest)
            clone_s = time.monotonic() - t0
            rows = log_commits(dest)
            if not rows:
                console.print(f"[yellow]{repo}: no commits found[/yellow]\n")
                continue
            life = metrics(rows)
            win = metrics(rows, since=since, until=until)
            _report(repo, life, win, win_years,
                    gh_numbers.get(repo.lower()), clone_s, len(rows))
        except Exception as e:  # noqa: BLE001 — prototype: report and continue
            console.print(f"[red]{repo}: {e}[/red]\n")
            exit_code = 1
        finally:
            shutil.rmtree(base, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
