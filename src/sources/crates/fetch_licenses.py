"""Add a `license` column to `data/sources/crates/results.csv` from the local DB dump.

Two-stage flow so the data survives `process_data.py` re-runs:

  1. Derive → `data/sources/crates/raw/licenses.csv` (persistent cache,
     365-day TTL from settings.json).
     Schema: package, license, fetched_at.
  2. Apply → joins the cache into `data/sources/crates/results.csv` as a
     `license` column. Re-runs after `process_data.py` rewrites
     `results.csv` cost zero dump reads — the apply step pulls from the cache.

`crates.io` requires every published crate to declare a license — it lives
on each version, in the `license` field of `versions.csv`. We use the
default version (same logic as `check_eol.py`) so the value matches what
`cargo install <crate>` would pull. Output is **lowercase SPDX** to match
the rest of the pipeline.

Common values: `mit`, `apache-2.0`, `mit or apache-2.0` (dual licensing is
the Rust default), `bsd-3-clause`, `unlicense or mit`.

The db-dump is gitignored and regenerable (see `fetch_db_dump.py`). While the
cache is fresh the dump is never opened, so this step also runs on a machine
that has no dump at all.

Usage:
    uv run python -m src.sources.crates.fetch_licenses                # cached + apply
    uv run python -m src.sources.crates.fetch_licenses --force        # ignore TTL
    uv run python -m src.sources.crates.fetch_licenses --apply-only   # skip dump, just join cache → results.csv
"""

import argparse
import csv
import datetime as dt
import logging
import os
import sys
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.params import fetch_ttl_days

csv.field_size_limit(sys.maxsize)

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
RESULTS = DATA_DIR / "sources" / "crates" / "results.csv"
RAW = DATA_DIR / "sources" / "crates" / "raw" / "licenses.csv"
DUMP = DATA_DIR / "sources" / "crates" / "db-dump"  # slim extracts (gitignored, regenerable), see fetch_db_dump

TTL_DAYS = fetch_ttl_days("sources/crates/fetch_licenses")  # 365 days, from settings.json
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


def dump_available() -> bool:
    """True when the slim db-dump extracts this script reads are on disk."""
    return all((DUMP / name).exists() for name in
               ("crates.csv", "default_versions.csv", "versions.csv"))


def load_license_index() -> dict[str, str]:
    """{crate_name_lowercase: lowercase SPDX} from the dump's default versions."""
    name_by_id: dict[str, str] = {}
    with open(DUMP / "crates.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name_by_id[r["id"]] = r["name"]

    default_by_crate: dict[str, str] = {}
    with open(DUMP / "default_versions.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            default_by_crate[r["crate_id"]] = r["version_id"]

    needed = set(default_by_crate.values())
    license_by_version: dict[str, str] = {}
    with open(DUMP / "versions.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["id"] in needed:
                license_by_version[r["id"]] = (r.get("license") or "").strip()

    out: dict[str, str] = {}
    for crate_id, vid in default_by_crate.items():
        name = name_by_id.get(crate_id)
        if name:
            out[name.lower()] = license_by_version.get(vid, "").lower()
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
        cached = cache.get(r["package"])
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
                   help="skip the db-dump read — only join the existing raw cache into results.csv")
    args = p.parse_args()

    console.rule("[bold cyan]crates/fetch_licenses")
    console.print(f"  raw cache: [dim]{RAW}[/dim]   TTL=[dim]{TTL_DAYS}d[/dim]")

    if not RESULTS.exists():
        raise SystemExit(f"missing {RESULTS}")

    cache = _load_raw_cache()
    console.print(f"  loaded [bold]{len(cache):,}[/bold] cached license rows from raw")

    if not args.apply_only:
        with open(RESULTS, encoding="utf-8") as f:
            packages = [r["package"] for r in csv.DictReader(f)]
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=TTL_DAYS)
        stale = [p for p in packages
                 if args.force
                 or p not in cache
                 or not _is_fresh(cache[p].get("fetched_at", ""), cutoff)]
        console.print(f"  scope: [bold]{len(packages):,}[/bold] packages, "
                      f"[yellow]{len(stale):,}[/yellow] need deriving")

        if stale and not dump_available():
            # The dump is regenerable scratch; a cache hit must not become a
            # hard failure. Keep the stale cache rather than blanking licenses.
            if not cache:
                raise SystemExit(
                    f"missing db-dump under {DUMP} and no cache at {RAW} — run "
                    "`uv run python -m src.sources.crates.fetch_db_dump` first")
            console.print(f"  [yellow]! db-dump absent ({DUMP}) — keeping the "
                          f"existing cache[/yellow]")
        elif stale:
            now = _now_iso()
            lic_idx = load_license_index()
            console.print(f"  read license for [bold]{len(lic_idx):,}[/bold] crates from db-dump")
            for pkg in stale:
                cache[pkg] = {"package": pkg,
                              "license": lic_idx.get(pkg.lower(), ""),
                              "fetched_at": now}
            _save_raw_cache(cache)
            console.print(f"  → updated [cyan]{RAW}[/cyan]")
        else:
            console.print("  [green]✓ raw cache is fresh — db-dump not read[/green]")

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
