"""Long-format CSV helpers for `data/git/{tool}.csv`.

All sha-pinned raw metric files share one schema:

    repo, repo_id, commit_sha, metric, value, checked_at

Key = (repo, commit_sha, metric). One row per metric per snapshot. New
runs upsert by key — historical snapshots for prior SHAs are preserved
as a time-series.

Empty `value` or empty `commit_sha` rows are dropped (no point storing a
metric with no value, or a snapshot we can't pin to a commit).

Numeric values are formatted with `_format_value` so floats round-trip
losslessly without spurious trailing zeros (`42` not `42.0` for ints,
`8.5` not `8.500000000001`). Strings pass through unchanged.

Usage:

    from src.git.long_format import upsert_snapshot

    upsert_snapshot(
        "data/git/lizard.csv",
        repo="aiohttp/aiohttp",
        repo_id="13392388",
        commit_sha="abc123…",
        metrics={"cognitive_total": 42, "cognitive_avg": 4.5},
        checked_at="2026-05-03T12:00:00Z",
    )
"""
from __future__ import annotations

import csv
import fcntl
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

FIELDS: tuple[str, ...] = (
    "repo", "repo_id", "commit_sha", "metric", "value", "checked_at",
)
KEY: tuple[str, str, str] = ("repo", "commit_sha", "metric")


def _format_value(value: Any) -> str:
    """Render a metric value as the canonical string for CSV storage.

    - bool → "True"/"False" (preserved for round-trip)
    - int → "42"
    - float → shortest representation (no trailing zeros, NaN/inf → "")
    - str → stripped, as-is
    - None → ""
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        # Shortest round-trip; drops trailing .0 only when it's truly an integer.
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).strip()


def _row_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (row["repo"], row["commit_sha"], row["metric"])


def read(path: str | Path) -> dict[tuple[str, str, str], dict[str, str]]:
    """Load existing rows keyed by (repo, commit_sha, metric).

    Returns empty dict if the file doesn't exist. Skips malformed rows.
    """
    p = Path(path)
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                key = _row_key(row)
            except KeyError:
                continue
            if not key[0] or not key[1] or not key[2]:
                continue
            out[key] = {k: row.get(k, "") for k in FIELDS}
    return out


@contextmanager
def _file_lock(path: str | Path):
    """Inter-process advisory lock on a sidecar `.lock` file.

    Required because long-format writers in concurrent fetchers all do
    read-modify-write on the same CSV. Without this lock, two workers
    that read the file before either writes will overwrite each other
    on flush — losing whichever ran first. fcntl.flock is sufficient
    here since the locking only needs to be cross-thread + cross-process
    on the same machine.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write(path: str | Path, rows: list[dict[str, str]]) -> None:
    """Write rows to a temp file in the same dir, then rename.

    Atomic at the filesystem level — readers either see the old file or
    the new one, never a partial write. Combined with `_file_lock`, this
    eliminates lost-write races between concurrent upserters.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(FIELDS))
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in FIELDS})
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_all(path: str | Path, rows: Iterable[dict[str, str]]) -> int:
    """Write rows in a stable order: (repo, commit_sha, metric) ascending.

    Atomic + lock-protected so concurrent upserters don't lose writes.
    Returns the number of rows written.
    """
    sorted_rows = sorted(rows, key=_row_key)
    with _file_lock(path):
        _atomic_write(path, sorted_rows)
    return len(sorted_rows)


def upsert_rows(path: str | Path, new_rows: Iterable[dict[str, str]]) -> int:
    """Merge `new_rows` into the file; new rows replace by (repo,sha,metric).

    Skips new rows where `value` or `commit_sha` is empty.
    Returns total row count written.

    Locks the file for the entire read-modify-write cycle so concurrent
    callers serialize and don't overwrite each other's contributions.
    """
    new_list = [
        r for r in new_rows
        if r.get("commit_sha") and r.get("value", "") != ""
    ]
    with _file_lock(path):
        existing = read(path)
        for row in new_list:
            key = _row_key(row)
            existing[key] = {k: row.get(k, "") for k in FIELDS}
        sorted_rows = sorted(existing.values(), key=_row_key)
        _atomic_write(path, sorted_rows)
    return len(sorted_rows)


def upsert_snapshot(
    path: str | Path,
    *,
    repo: str,
    repo_id: str,
    commit_sha: str,
    metrics: dict[str, Any],
    checked_at: str,
) -> int:
    """Upsert one repo's snapshot — a metric→value dict at a single sha.

    Empty/None metric values are skipped (no row written for them).
    """
    if not commit_sha:
        return 0
    new_rows: list[dict[str, str]] = []
    for metric, value in metrics.items():
        formatted = _format_value(value)
        if formatted == "":
            continue
        new_rows.append({
            "repo": repo,
            "repo_id": str(repo_id) if repo_id else "",
            "commit_sha": commit_sha,
            "metric": metric,
            "value": formatted,
            "checked_at": checked_at,
        })
    return upsert_rows(path, new_rows)


def latest_sha_per_repo(
    path: str | Path,
    sha_priority: list[str] | None = None,
) -> dict[str, str]:
    """For each repo, pick its preferred snapshot SHA from the long file.

    If `sha_priority` is given (e.g. last_sha for 2025, 2024, 2023…),
    returns the first sha from that list that has any rows for the repo.
    Otherwise returns the lexicographically-first sha found per repo
    (deterministic but arbitrary). Callers usually pass an explicit
    priority list from `commits-years.csv`.
    """
    rows = read(path)
    if sha_priority:
        priority_set = set(sha_priority)
        repo_shas: dict[str, set[str]] = {}
        for (repo, sha, _metric) in rows.keys():
            repo_shas.setdefault(repo, set()).add(sha)
        out: dict[str, str] = {}
        for repo, shas in repo_shas.items():
            for s in sha_priority:
                if s in shas:
                    out[repo] = s
                    break
        return out
    # Fallback: deterministic but arbitrary
    out2: dict[str, str] = {}
    for (repo, sha, _metric) in rows.keys():
        if repo not in out2 or sha < out2[repo]:
            out2[repo] = sha
    return out2


def project_to_wide(
    path: str | Path,
    repo_sha: dict[str, str],
    metrics: list[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Project long → wide: {repo: {metric: value}} for the chosen sha per repo.

    `repo_sha` maps each repo to the snapshot SHA we want metrics for.
    `metrics`, if given, restricts the output to those metric names.
    Repos without rows for the requested sha get an empty dict.
    """
    rows = read(path)
    metric_filter = set(metrics) if metrics else None
    out: dict[str, dict[str, str]] = {r: {} for r in repo_sha}
    for (repo, sha, metric), row in rows.items():
        if repo not in repo_sha:
            continue
        if sha != repo_sha[repo]:
            continue
        if metric_filter is not None and metric not in metric_filter:
            continue
        out[repo][metric] = row["value"]
    return out
