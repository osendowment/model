"""Add a `license` column to `data/sources/cpp/results.csv` by joining Homebrew.

`cpp` is a synthetic ecosystem unifying Debian + Homebrew C/C++ packages.
Debian's `debian/copyright` files are unstructured prose and don't yield
clean SPDX, but the same project usually ships as a Homebrew formula and
Homebrew **does** declare an SPDX license (see `homebrew/fetch_licenses.py`).

Strategy: join cpp `package` ↔ homebrew `formula` by name (case-insensitive).
Anything unmatched falls back to empty — eligibility's GitHub-API license
will catch it.

Two-stage flow so the data survives `process_data.py` re-runs:

  1. Derive → `data/sources/cpp/raw/licenses.csv` (persistent cache, 365-day
     TTL from settings.json).
     Schema: package, license, fetched_at.
  2. Apply → joins the cache into `data/sources/cpp/results.csv` as a
     `license` column. Re-runs after `process_data.py` rewrites `results.csv`
     cost zero Homebrew reads — the apply step pulls from the cache.

The derive step reads homebrew's `results.csv` `license` column, and falls
back to homebrew's own `raw/licenses.csv` cache when a `process_data.py`
rebuild has dropped that column — the same values, one hop upstream.

Run order:
    uv run python -m src.sources.homebrew.fetch_licenses   # populate first
    uv run python -m src.sources.cpp.fetch_licenses

Usage:
    uv run python -m src.sources.cpp.fetch_licenses                # cached + apply
    uv run python -m src.sources.cpp.fetch_licenses --force        # ignore TTL
    uv run python -m src.sources.cpp.fetch_licenses --apply-only   # skip homebrew, just join cache → results.csv
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
CPP_RESULTS = DATA_DIR / "sources" / "cpp" / "results.csv"
RAW = DATA_DIR / "sources" / "cpp" / "raw" / "licenses.csv"
HOMEBREW_RESULTS = DATA_DIR / "sources" / "homebrew" / "results.csv"
HOMEBREW_RAW = DATA_DIR / "sources" / "homebrew" / "raw" / "licenses.csv"

TTL_DAYS = fetch_ttl_days("sources/cpp/fetch_licenses")  # 365 days, from settings.json
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


def load_homebrew_licenses() -> tuple[dict[str, str], str]:
    """{formula_lowercase: lowercase SPDX} + a label for where it came from.

    Prefers homebrew's enriched `results.csv`; when that table carries no
    `license` column (a `process_data.py` rebuild dropped it), reads
    homebrew's durable `raw/licenses.csv` cache instead. Returns an empty
    index and an empty label when neither source holds a license.
    """
    if HOMEBREW_RESULTS.exists():
        out: dict[str, str] = {}
        with open(HOMEBREW_RESULTS, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                name = (r.get("formula") or r.get("package") or "").lower()
                lic = (r.get("license") or "").strip().lower()
                if name and lic:
                    out[name] = lic
        if out:
            return out, "homebrew results.csv"

    if HOMEBREW_RAW.exists():
        out = {}
        with open(HOMEBREW_RAW, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                name = (r.get("package") or "").strip().lower()
                lic = (r.get("license") or "").strip().lower()
                if name and lic:
                    out[name] = lic
        if out:
            return out, "homebrew raw/licenses.csv"

    return {}, ""


def _apply_to_results(cache: dict[str, dict]) -> tuple[int, int]:
    """Join `license` column from cache into results.csv. Returns (populated, total)."""
    with open(CPP_RESULTS, encoding="utf-8") as f:
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

    tmp = CPP_RESULTS.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, CPP_RESULTS)
    return populated, len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--force", action="store_true",
                   help=f"ignore the {TTL_DAYS}-day TTL, re-derive everything")
    p.add_argument("--apply-only", action="store_true",
                   help="skip the homebrew join — only join the existing raw cache into results.csv")
    args = p.parse_args()

    console.rule("[bold cyan]cpp/fetch_licenses")
    console.print(f"  raw cache: [dim]{RAW}[/dim]   TTL=[dim]{TTL_DAYS}d[/dim]")

    if not CPP_RESULTS.exists():
        raise SystemExit(f"missing {CPP_RESULTS}")

    cache = _load_raw_cache()
    console.print(f"  loaded [bold]{len(cache):,}[/bold] cached license rows from raw")

    if not args.apply_only:
        with open(CPP_RESULTS, encoding="utf-8") as f:
            packages = [r["package"] for r in csv.DictReader(f)]
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=TTL_DAYS)
        stale = [p for p in packages
                 if args.force
                 or p not in cache
                 or not _is_fresh(cache[p].get("fetched_at", ""), cutoff)]
        console.print(f"  scope: [bold]{len(packages):,}[/bold] packages, "
                      f"[yellow]{len(stale):,}[/yellow] need deriving")

        if stale:
            lic_idx, src = load_homebrew_licenses()
            if not lic_idx:
                # No upstream licenses at all: keep the cache rather than
                # overwrite good values with blanks.
                if not cache:
                    raise SystemExit(
                        f"no homebrew licenses in {HOMEBREW_RESULTS} or "
                        f"{HOMEBREW_RAW} — run "
                        "`uv run python -m src.sources.homebrew.fetch_licenses` first")
                console.print("  [yellow]! no homebrew licenses available — "
                              "keeping the existing cache[/yellow]")
            else:
                now = _now_iso()
                console.print(f"  joined against [bold]{len(lic_idx):,}[/bold] "
                              f"homebrew licenses [dim]({src})[/dim]")
                for pkg in stale:
                    cache[pkg] = {"package": pkg,
                                  "license": lic_idx.get(pkg.lower(), ""),
                                  "fetched_at": now}
                _save_raw_cache(cache)
                console.print(f"  → updated [cyan]{RAW}[/cyan]")
        else:
            console.print("  [green]✓ raw cache is fresh — homebrew not read[/green]")

    # Always apply cache → results.csv (idempotent)
    populated, total = _apply_to_results(cache)
    pct = 100 * populated / max(total, 1)
    console.print(f"  applied to results.csv: [bold green]{populated:,}[/bold green]/{total:,} ({pct:.1f}%)")

    ctr = Counter()
    with open(CPP_RESULTS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ctr[r.get("license") or "(none)"] += 1
    tbl = Table(title="[bold]Top licenses[/bold]", header_style="bold dim")
    tbl.add_column("license"); tbl.add_column("count", justify="right")
    for lic, n in ctr.most_common(15):
        tbl.add_row(lic, f"{n:,}")
    console.print(tbl)


if __name__ == "__main__":
    main()
