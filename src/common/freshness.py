"""Shared TTL / freshness gating for the funding-intent discovery layer.

Every funding source shares ONE TTL window (`FUNDING_TTL_DAYS`) so discovery is
**idempotent within the window**: a re-run inside TTL finds every record fresh,
fetches nothing, and rewrites nothing — identical output. A record whose status
column holds an error value is never fresh, so a transient failure is always
retried rather than cached as a genuine "no funding" result (auditability).

Two gates, matching the two fetch shapes in this codebase:

- `row_is_fresh(row, …)` — per-row, keyed on the row's `fetched_at` timestamp.
  Used by the incremental gap-fill fetchers (GitHub / Open Collective GraphQL,
  npm / PyPI registry lookups) that skip individual fresh rows.
- `file_is_fresh(path, …)` — whole-file, keyed on the output file's mtime. Used
  by the bulk single-download fetchers (FLOSS Fund tarball, the OC reverse
  index) that refresh the entire file at once.
"""
from __future__ import annotations

import datetime
import os
import time

# One TTL for the entire funding-intent discovery layer (days). Within this
# window a re-run of any funding fetcher is a no-op — the idempotency guarantee.
FUNDING_TTL_DAYS = 365

# Status values that mark a record as a failed fetch (never fresh → always retried).
ERROR_STATUSES = ("error",)


def row_is_fresh(
    row: dict,
    ttl_days: int = FUNDING_TTL_DAYS,
    *,
    status_key: str | None = None,
    error_values: tuple[str, ...] = ERROR_STATUSES,
) -> bool:
    """True if `row`'s `fetched_at` is within `ttl_days`.

    When `status_key` is given, a row whose status holds an `error_values` entry
    is treated as stale (always retried) — a failed fetch must not be cached for
    the full TTL.
    """
    if status_key:
        status = (row.get(status_key) or "").strip().lower()
        if status in error_values:
            return False
    ts = (row.get("fetched_at") or "").strip()
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    return dt >= cutoff


def file_is_fresh(path: str | os.PathLike, ttl_days: int = FUNDING_TTL_DAYS) -> bool:
    """True if `path` exists and was modified within `ttl_days`.

    Whole-file TTL for bulk single-download sources. `ttl_days <= 0` forces a
    refresh (the conventional `--ttl 0` / `--force` escape hatch).
    """
    if ttl_days <= 0 or not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < ttl_days * 86400
