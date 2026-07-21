"""Shared TTL / freshness gating for the funding-intent discovery layer.

Funding records are cached on **two** windows. A record that already carries a
funding signal uses the full `FUNDING_TTL_DAYS` window — declared funding rarely
disappears, so re-running inside it is a no-op (idempotency for *positive*
results). A record with no signal yet uses the shorter
`FUNDING_EMPTY_RECHECK_DAYS` window (`funding_ttl_for` maps the per-row boolean
to the right one): the empties are the rows most likely to gain funding and the
cheapest to re-query, so they self-heal instead of staying stale for a full year.
A record whose status column holds an error value is never fresh, so a transient
failure is always retried rather than cached as a genuine "no funding" result
(auditability).

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

from src.common.params import fetch_retry_days, fetch_ttl_days

# One TTL for the entire funding-intent discovery layer (days). A record that
# already carries a funding signal is cached for this full window — declared
# funding rarely disappears, so re-running inside it is a no-op (the idempotency
# guarantee for *positive* results). The window is 365 days and comes from
# settings.json (`fetch_ttl_days`).
FUNDING_TTL_DAYS = fetch_ttl_days("common/freshness")

# A record with NO funding signal yet (`has_funding_links=False`, 0 sponsors, …)
# is the one most likely to GAIN one and the cheapest to re-query, so it is
# rechecked on this much shorter window instead of the full TTL. This is what
# keeps a freshly-added FUNDING.yml from staying invisible for up to a year
# (the canonical miss: rust-lang/regex added a FUNDING.yml 1.5h after a fetch).
# It is a RETRY back-off (30 days), not a cache TTL, so it comes from
# settings.json `fetch_retry_days`.
FUNDING_EMPTY_RECHECK_DAYS = fetch_retry_days("common/freshness.funding_empty_recheck")

# Status values that mark a record as a failed fetch (never fresh → always retried).
ERROR_STATUSES = ("error",)


def funding_ttl_for(has_signal: bool) -> int:
    """Per-row funding TTL: the full window for a row that already carries a
    funding signal, the shorter recheck window for an empty/negative result.

    The caller decides what "has a signal" means for its source (a funding link,
    a non-zero sponsor count, an active sponsors listing); this just maps that
    boolean to the right window so positive results stay idempotent while
    negatives self-heal."""
    return FUNDING_TTL_DAYS if has_signal else FUNDING_EMPTY_RECHECK_DAYS


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
