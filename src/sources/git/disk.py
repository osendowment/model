"""Disk-space safety helpers shared by every fetcher that clones repos.

Every per-repo fetcher (`fetch_scc`, `fetch_sha_metrics`,
`fetch_churn`, `fetch_gitpandas`)
writes hundreds of MB to /tmp at peak. The user runs on a small SSD,
so we want two safety nets that are cheap to wire in:

1. ``print_disk_banner(tmp_dir, min_free_gb)``
   Logs free / total disk on ``tmp_dir`` at startup. Warns (yellow)
   if free < ``min_free_gb`` and red-warns + raises if free is so low
   the run would almost certainly fail. Call this first thing in
   every fetcher's ``main()``.

2. ``check_disk_or_exit(min_gb, tmp_dir, console=...)``
   Cheap stdlib ``shutil.disk_usage`` call. Returns ``True`` while
   free disk is OK; returns ``False`` on the first call where free
   disk dips below ``min_gb``. Callers poll this between repos /
   between batches and break the loop gracefully when it flips. We
   deliberately don't raise — the caller already has half-written
   results to flush before exiting.

Stdlib only — ``shutil.disk_usage`` is portable and accurate enough.
We avoid ``psutil`` so the dependency surface stays flat.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time

from rich.console import Console

# Default to the actual location `tempfile.mkdtemp` will use. On macOS this
# is `/var/folders/.../T/` (per-user, not `/tmp`) — passing `/tmp` to the
# checks would monitor a different filesystem and miss disk pressure where
# clones actually accumulate.
_DEFAULT_TMP = tempfile.gettempdir()

# Every clone fetcher routes its base temp dir through `make_clone_tmpdir`,
# so all our clone temp dirs share this prefix. `sweep_stale_clone_dirs`
# reclaims orphans left by crashed runs by matching exactly this prefix.
CLONE_TMP_PREFIX = "ose-fetch-"

log = logging.getLogger(__name__)


def disk_usage_gb(path: str = _DEFAULT_TMP) -> tuple[float, float]:
    """Return ``(free_gb, total_gb)`` for the filesystem hosting ``path``.

    Falls back to ``(0.0, 0.0)`` if ``shutil.disk_usage`` raises (e.g.
    the path doesn't exist yet) — callers should treat 0.0 as "unknown,
    don't gate on it".
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        log.debug("disk_usage_gb(%s) failed: %s", path, e)
        return 0.0, 0.0
    return usage.free / 1e9, usage.total / 1e9


def print_disk_banner(
    tmp_dir: str = _DEFAULT_TMP,
    min_free_gb: float = 5.0,
    console: Console | None = None,
) -> None:
    """Print a one-line free-disk banner; warn (yellow) if below ``min_free_gb``.

    Always rich-prints to stderr-equivalent (rich Console default) so it
    appears alongside the existing fetcher startup banner. The exact
    threshold is informational — actual abort is controlled by
    ``check_disk_or_exit`` via the per-fetcher ``--max-disk-gb`` flag.
    """
    console = console or Console()
    free_gb, total_gb = disk_usage_gb(tmp_dir)
    if total_gb <= 0:
        console.print(f"[dim]Disk[/dim] [yellow]could not stat {tmp_dir}[/yellow]")
        return
    used_pct = 100.0 * (1.0 - free_gb / total_gb) if total_gb > 0 else 0.0
    style = "dim"
    label = "ok"
    if free_gb < min_free_gb:
        style = "yellow"
        label = f"low (< {min_free_gb:.0f} GB)"
    if free_gb < min_free_gb / 2:
        style = "red"
        label = f"critical (< {min_free_gb / 2:.1f} GB)"
    console.print(
        f"[dim]Disk[/dim] [{style}]{free_gb:.1f} GB free[/{style}] "
        f"[dim]of {total_gb:.0f} GB on {tmp_dir} "
        f"({used_pct:.0f}% used) — {label}[/dim]"
    )


def check_disk_or_exit(
    min_gb: float,
    tmp_dir: str = _DEFAULT_TMP,
    console: Console | None = None,
) -> bool:
    """Return True if free disk on ``tmp_dir`` ≥ ``min_gb``; else log and return False.

    Callers poll between repos so half-baked results can flush. The
    first time this returns False, the caller should:
      1. log the warning (we already print one here),
      2. stop submitting new clones,
      3. wait for in-flight workers to finish (or cancel them — your call),
      4. flush partial results to disk, exit cleanly.

    This function never raises and never sleeps — it's a pure check.
    """
    free_gb, total_gb = disk_usage_gb(tmp_dir)
    if total_gb <= 0:
        # Couldn't stat — fail open (don't block the run on a missing dir).
        return True
    if free_gb >= min_gb:
        return True
    console = console or Console()
    console.print(
        f"[red]Disk low[/red] [dim]on {tmp_dir}: "
        f"{free_gb:.1f} GB free < {min_gb:.1f} GB threshold — "
        f"finishing in-flight repos and exiting.[/dim]"
    )
    return False


# ── clone temp-dir lifecycle ────────────────────────────────────────────────
#
# Clone fetchers write hundreds of MB per repo into a base temp dir and clean
# it up in a `finally`. A hard kill (SIGKILL, OOM, power loss) skips that —
# the base dir and its in-flight clones leak and, run after run, exhaust temp
# storage. Two helpers close the gap: every fetcher creates its base dir via
# `make_clone_tmpdir` (shared `CLONE_TMP_PREFIX`) and calls
# `sweep_stale_clone_dirs` at startup to reclaim orphans from crashed runs.


def make_clone_tmpdir(tag: str) -> str:
    """Create a clone fetcher's base temp dir, namespaced for auto-sweep.

    `tag` identifies the fetcher (e.g. "scc", "churn") — purely for human
    readability; the sweep keys off `CLONE_TMP_PREFIX` alone.
    """
    return tempfile.mkdtemp(prefix=f"{CLONE_TMP_PREFIX}{tag}-")


def sweep_stale_clone_dirs(
    max_age_minutes: float = 60.0,
    tmp_dir: str = _DEFAULT_TMP,
    console: Console | None = None,
) -> int:
    """Remove orphaned `CLONE_TMP_PREFIX` temp dirs left by crashed runs.

    Only dirs whose mtime is older than `max_age_minutes` are removed — an
    actively-running fetcher constantly creates/removes per-repo subdirs in
    its base dir, keeping that dir's mtime fresh, so a concurrent live run
    is never deleted. Call once at fetcher startup. Returns the count removed.
    """
    cutoff = time.time() - max_age_minutes * 60.0
    removed = 0
    try:
        entries = os.listdir(tmp_dir)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith(CLONE_TMP_PREFIX):
            continue
        path = os.path.join(tmp_dir, name)
        if not os.path.isdir(path):
            continue
        try:
            if os.path.getmtime(path) >= cutoff:
                continue  # recently touched — possibly a live run
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    if removed:
        console = console or Console()
        console.print(
            f"[dim]Temp sweep[/dim] reclaimed [yellow]{removed}[/yellow] "
            f"stale clone dir(s) [dim]from crashed prior runs[/dim]"
        )
    return removed
