"""Plumb licenses from `data/sources/homebrew/raw/formulas.csv` into `results.csv`.

Homebrew formulas declare an SPDX license natively (`Formula#license` in
the Ruby DSL). It's already collected — `data/sources/homebrew/raw/formulas.csv`
has the column. This script joins it into the per-formula results table and
lower-cases for consistency.

Two-stage flow so the data survives `process_data.py` re-runs:

  1. Derive → `data/sources/homebrew/raw/licenses.csv` (persistent cache,
     365-day TTL from settings.json).
     Schema: package, license, fetched_at.
  2. Apply → joins the cache into `data/sources/homebrew/results.csv` as a
     `license` column. Re-runs after `process_data.py` rewrites
     `results.csv` cost zero formula reads — the apply step pulls from the cache.

Output: `data/sources/homebrew/results.csv` gains a `license` column.

Usage:
    uv run python -m src.sources.homebrew.fetch_licenses                # cached + apply
    uv run python -m src.sources.homebrew.fetch_licenses --force        # ignore TTL
    uv run python -m src.sources.homebrew.fetch_licenses --apply-only   # skip formulas, just join cache → results.csv
"""

import argparse
import csv
import datetime as dt
import logging
import os
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.params import fetch_ttl_days

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
RESULTS = DATA_DIR / "sources" / "homebrew" / "results.csv"
FORMULAS = DATA_DIR / "sources" / "homebrew" / "raw" / "formulas.csv"
RAW = DATA_DIR / "sources" / "homebrew" / "raw" / "licenses.csv"

TTL_DAYS = fetch_ttl_days("sources/homebrew/fetch_licenses")  # 365 days, from settings.json
RAW_FIELDS = ["package", "license", "fetched_at"]


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_fresh(ts: str, cutoff: dt.datetime) -> bool:
    if not ts:
        return False
    try:
        when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        return when >= cutoff
    except Exception:
        return False


def _load_raw_cache() -> dict[str, dict]:
    """Return {package: {license, fetched_at}} from raw/licenses.csv."""
    if not RAW.exists():
        return {}
    out: dict[str, dict] = {}
    with open(RAW, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pkg = (r.get("package") or "").strip()
            if pkg:
                out[pkg] = r
    return out


def _save_raw_cache(cache: dict[str, dict]) -> None:
    RAW.parent.mkdir(parents=True, exist_ok=True)
    tmp = RAW.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RAW_FIELDS, quoting=csv.QUOTE_MINIMAL,
                           extrasaction="ignore")
        w.writeheader()
        for pkg in sorted(cache.keys()):
            w.writerow(cache[pkg])
    os.replace(tmp, RAW)


def _results_key(row: dict) -> str:
    """The formula name of a results.csv row — the cache key."""
    # Homebrew results.csv keys on `formula` in some builds, `package` in others.
    return (row.get("formula") or row.get("package") or "").strip()


def load_license_index() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(FORMULAS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name = (r.get("name") or "").strip().lower()
            lic = (r.get("license") or "").strip().lower()
            if name:
                out[name] = lic
    return out


def _apply_to_results(cache: dict[str, dict]) -> tuple[int, int]:
    """Join `license` column from cache into results.csv. Returns (populated, total)."""
    with open(RESULTS, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys()) if rows else []
    if "license" not in fields:
        fields.append("license")

    populated = 0
    for r in rows:
        cached = cache.get(_results_key(r))
        lic = (cached.get("license") if cached else "") or ""
        r["license"] = lic
        if lic:
            populated += 1

    tmp = RESULTS.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, RESULTS)
    return populated, len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--force", action="store_true",
                   help=f"ignore the {TTL_DAYS}-day TTL, re-derive everything")
    p.add_argument("--apply-only", action="store_true",
                   help="skip raw/formulas.csv — only join the existing raw cache into results.csv")
    args = p.parse_args()

    console.rule("[bold cyan]homebrew/fetch_licenses")
    console.print(f"  raw cache: [dim]{RAW}[/dim]   TTL=[dim]{TTL_DAYS}d[/dim]")

    if not RESULTS.exists():
        raise SystemExit(f"missing {RESULTS}")

    cache = _load_raw_cache()
    console.print(f"  loaded [bold]{len(cache):,}[/bold] cached license rows from raw")

    if not args.apply_only:
        with open(RESULTS, encoding="utf-8") as f:
            packages = [_results_key(r) for r in csv.DictReader(f)]
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=TTL_DAYS)
        stale = [p for p in packages
                 if p and (args.force
                           or p not in cache
                           or not _is_fresh(cache[p].get("fetched_at", ""), cutoff))]
        console.print(f"  scope: [bold]{len(packages):,}[/bold] formulas, "
                      f"[yellow]{len(stale):,}[/yellow] need deriving")

        if stale and not FORMULAS.exists():
            # formulas.csv is a fetched artefact; a cache hit must not become a
            # hard failure. Keep the stale cache rather than blanking licenses.
            if not cache:
                raise SystemExit(f"missing {FORMULAS} and no cache at {RAW}")
            console.print(f"  [yellow]! {FORMULAS} absent — keeping the "
                          f"existing cache[/yellow]")
        elif stale:
            now = _now_iso()
            lic_idx = load_license_index()
            console.print(f"  read license for [bold]{len(lic_idx):,}[/bold] formulas from raw")
            for pkg in stale:
                cache[pkg] = {"package": pkg,
                              "license": lic_idx.get(pkg.lower(), ""),
                              "fetched_at": now}
            _save_raw_cache(cache)
            console.print(f"  → updated [cyan]{RAW}[/cyan]")
        else:
            console.print("  [green]✓ raw cache is fresh — formulas.csv not read[/green]")

    # Always apply cache → results.csv (idempotent)
    populated, total = _apply_to_results(cache)
    pct = 100 * populated / max(total, 1)
    console.print(f"  applied to results.csv: [bold green]{populated:,}[/bold green]/{total:,} ({pct:.1f}%)")

    ctr = Counter()
    with open(RESULTS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ctr[r.get("license") or "(none)"] += 1
    tbl = Table(title="[bold]Top licenses[/bold]", header_style="bold dim")
    tbl.add_column("license"); tbl.add_column("count", justify="right")
    for lic, n in ctr.most_common(15):
        tbl.add_row(lic, f"{n:,}")
    console.print(tbl)


if __name__ == "__main__":
    main()
