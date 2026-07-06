#!/usr/bin/env python3
"""Fetch the full SPDX License List (Linux Foundation) and write to CSV.

The SPDX License List is the canonical machine-readable license registry,
maintained by the SPDX project under the Linux Foundation. Every license
carries two independent approval flags:

  isOsiApproved — the license passed the Open Source Initiative's formal
                  license-review process (the OSI "Open Source Definition").
  isFsfLibre    — the Free Software Foundation lists it as a free
                  software/content license (gnu.org/licenses/license-list).

This fetcher stores the WHOLE list — every SPDX id with both flags — as
`data/sources/spdx/licenses.csv`. It takes no policy position: the unified
OSS-approved set (OSI ∪ FSF-libre software licenses ∪ curated extras) is
derived from this file by `src.sources.osi.fetch_licenses`, which also
prints the OSI-vs-FSF comparison.

Output columns:
  spdx_id           — lowercased SPDX ID (the join key, e.g. `apache-2.0`)
  spdx_id_canonical — original SPDX casing (`Apache-2.0`)
  name              — full human-readable name
  is_osi_approved   — True/False (SPDX `isOsiApproved`)
  is_fsf_libre      — True/False (SPDX `isFsfLibre`; absent → False)
  is_deprecated     — True if SPDX deprecated this ID
  reference         — https://spdx.org/licenses/{id}.html
  osi_url           — first opensource.org/license link from `seeAlso`, if any
  fetched_at        — UTC ISO 8601 timestamp

90-day TTL — re-runs within the window are no-ops unless `--force`.

Usage:
    uv run python -m src.sources.spdx.fetch_licenses
    uv run python -m src.sources.spdx.fetch_licenses --force
"""

import argparse
import csv
import datetime as dt
import logging
import os
from pathlib import Path

import httpx
from rich.console import Console

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "sources" / "spdx"
OUT = DATA_DIR / "licenses.csv"

SPDX_LIST_URL = (
    "https://raw.githubusercontent.com/spdx/license-list-data/main/json/licenses.json"
)
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"
TTL_DAYS = 90

FIELDS = ["spdx_id", "spdx_id_canonical", "name",
          "is_osi_approved", "is_fsf_libre", "is_deprecated",
          "reference", "osi_url", "fetched_at"]


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_rows(data: dict, now: str | None = None) -> list[dict]:
    """The SPDX licenses.json payload → one CSV row per license, sorted."""
    now = now or _now_iso()
    rows = []
    for lic in data.get("licenses", []):
        spdx = lic.get("licenseId", "")
        if not spdx:
            continue
        osi_url = ""
        for u in lic.get("seeAlso") or []:
            if "opensource.org/license" in u or "opensource.org/licenses" in u:
                osi_url = u
                break
        rows.append({
            "spdx_id": spdx.lower(),
            "spdx_id_canonical": spdx,
            "name": lic.get("name", ""),
            "is_osi_approved": bool(lic.get("isOsiApproved", False)),
            "is_fsf_libre": bool(lic.get("isFsfLibre", False)),
            "is_deprecated": bool(lic.get("isDeprecatedLicenseId", False)),
            "reference": lic.get("reference", ""),
            "osi_url": osi_url,
            "fetched_at": now,
        })
    rows.sort(key=lambda r: r["spdx_id"])
    return rows


def fetch() -> list[dict]:
    log.info("fetching SPDX license list ...")
    resp = httpx.get(SPDX_LIST_URL, timeout=30,
                     headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return build_rows(resp.json())


def _is_fresh(path: Path) -> bool:
    """True if `path` exists and its newest `fetched_at` is within TTL."""
    if not path.exists():
        return False
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=TTL_DAYS)
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts = r.get("fetched_at", "")
            if not ts:
                continue
            try:
                when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.UTC)
                return when >= cutoff
            except Exception:
                pass
    return False


def _write(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, OUT)


def load(path: Path = OUT) -> list[dict]:
    """The stored SPDX list; empty when never fetched."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ensure(force: bool = False, verbose: bool = True) -> Path:
    """Ensure `data/sources/spdx/licenses.csv` exists and is fresh.

    Called by the OSS-set builder (`src.sources.osi.fetch_licenses`) so the
    chain is self-bootstrapping. Honors the 90-day TTL; `force=True` refetches.
    """
    if not force and _is_fresh(OUT):
        if verbose:
            console.print(f"[dim]SPDX list fresh (≤{TTL_DAYS}d) → {OUT}[/dim]")
        return OUT
    rows = fetch()
    _write(rows)
    if verbose:
        n_osi = sum(1 for r in rows if r["is_osi_approved"])
        n_fsf = sum(1 for r in rows if r["is_fsf_libre"])
        console.print(f"[green]SPDX list: {len(rows)} licenses "
                      f"({n_osi} OSI-approved, {n_fsf} FSF-libre) → {OUT}[/green]")
    return OUT


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--force", action="store_true", help="ignore the 90-day TTL")
    args = p.parse_args()
    ensure(force=args.force)


if __name__ == "__main__":
    main()
