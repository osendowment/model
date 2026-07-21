"""Fetch SPDX licenses for every package in `data/sources/npm/results.csv`.

Two-stage flow so the data survives `process_data.py` re-runs:

  1. Fetch → `data/sources/npm/raw/licenses.csv` (persistent cache, 365-day
     TTL from settings.json).
     Schema: package, license, fetched_at.
  2. Apply → joins the raw cache into `data/sources/npm/results.csv` as a
     `license` column. Re-runs after `process_data.py` rewrites
     `results.csv` cost zero API calls — the apply step pulls from raw.

Source: `registry.npmjs.org/{pkg}` (full metadata endpoint, since the
abbreviated `vnd.npm.install-v1+json` doesn't include the license).

We pick the license off the version pointed to by `dist-tags.latest` —
that's what `npm install <pkg>` resolves to and the canonical maintainer
declaration. Fallback to the package-level `license` field when the
version-level one is missing.

Output is **lowercase SPDX**. npm publishers sometimes use legacy formats:

  • String SPDX:                  `"MIT"`           → `mit`
  • SPDX expression:              `"(MIT OR Apache-2.0)"` → `mit or apache-2.0`
  • Object form:                  `{"type": "BSD"}` → `bsd`
  • Array of objects (legacy):    `[{"type": "MIT"}, ...]` → `mit | apache-2.0`
  • `"SEE LICENSE IN <file>"`     → kept verbatim, lowercased

Async with 80-way concurrency. ~6,400 packages × ~150ms / 80 ≈ 15s typical.

Usage:
    uv run python -m src.sources.npm.fetch_licenses                # cached + apply
    uv run python -m src.sources.npm.fetch_licenses --force        # ignore TTL
    uv run python -m src.sources.npm.fetch_licenses --apply-only   # skip API, just join cache → results.csv
    uv run python -m src.sources.npm.fetch_licenses --limit 100
    uv run python -m src.sources.npm.fetch_licenses --concurrency 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import logging
import os
from collections import Counter
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.table import Table
from tqdm.asyncio import tqdm_asyncio

from src.common.params import fetch_ttl_days

logging.basicConfig(level="WARNING")
log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
RESULTS = DATA_DIR / "sources" / "npm" / "results.csv"
RAW = DATA_DIR / "sources" / "npm" / "raw" / "licenses.csv"

REGISTRY = "https://registry.npmjs.org"
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"
TTL_DAYS = fetch_ttl_days("sources/npm/fetch_licenses")  # 365 days, from settings.json
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


def _normalise(raw) -> str:
    """Convert npm's various license shapes into a lower-cased string."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip().lower()
    if isinstance(raw, dict):
        return str(raw.get("type", "")).strip().lower()
    if isinstance(raw, list):
        # Legacy `licenses: [{type: ...}, ...]`
        parts = [str((x.get("type") if isinstance(x, dict) else x) or "").strip()
                 for x in raw]
        return " | ".join(p for p in parts if p).lower()
    return str(raw).strip().lower()


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


async def _fetch(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                 pkg: str) -> tuple[str, str]:
    url = f"{REGISTRY}/{pkg.replace('/', '%2F')}"
    async with sem:
        for attempt in range(3):
            try:
                async with session.get(
                    url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 404:
                        return pkg, ""
                    if resp.status != 200:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    data = await resp.json()
                    latest = (data.get("dist-tags") or {}).get("latest")
                    version_meta = ((data.get("versions") or {}).get(latest) or {}) if latest else {}
                    return pkg, _normalise(
                        version_meta.get("license")
                        or data.get("license")
                        or version_meta.get("licenses")
                        or data.get("licenses")
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(0.5 * (attempt + 1))
        return pkg, ""


async def _collect(packages: list[str], concurrency: int) -> dict[str, str]:
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, str] = {}
    async with aiohttp.ClientSession() as session:
        coros = [_fetch(session, sem, p) for p in packages]
        for fut in tqdm_asyncio.as_completed(
            coros, total=len(coros), desc="npm licenses"
        ):
            pkg, lic = await fut
            out[pkg] = lic
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
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=80)
    p.add_argument("--force", action="store_true",
                   help=f"ignore the {TTL_DAYS}-day TTL, refetch everything")
    p.add_argument("--apply-only", action="store_true",
                   help="skip the API fetch — only join the existing raw cache into results.csv")
    args = p.parse_args()

    console.rule("[bold cyan]npm/fetch_licenses")
    console.print(f"  raw cache: [dim]{RAW}[/dim]   TTL=[dim]{TTL_DAYS}d[/dim]")

    if not RESULTS.exists():
        raise SystemExit(f"missing {RESULTS}")

    cache = _load_raw_cache()
    console.print(f"  loaded [bold]{len(cache):,}[/bold] cached license rows from raw")

    if not args.apply_only:
        # Determine packages needing fetch (missing from cache or stale)
        with open(RESULTS, encoding="utf-8") as f:
            packages = [r["package"] for r in csv.DictReader(f)]
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=TTL_DAYS)
        to_fetch = [p for p in packages
                    if args.force
                    or p not in cache
                    or not _is_fresh(cache[p].get("fetched_at", ""), cutoff)]
        if args.limit:
            to_fetch = to_fetch[: args.limit]
        console.print(f"  scope: [bold]{len(packages):,}[/bold] packages, "
                      f"[yellow]{len(to_fetch):,}[/yellow] need fetching "
                      f"(concurrency={args.concurrency})")

        if to_fetch:
            now = _now_iso()
            results = asyncio.run(_collect(to_fetch, args.concurrency))
            for pkg, lic in results.items():
                cache[pkg] = {"package": pkg, "license": lic, "fetched_at": now}
            _save_raw_cache(cache)
            console.print(f"  → updated [cyan]{RAW}[/cyan]")
        else:
            console.print("  [green]✓ raw cache is fresh — no API calls needed[/green]")

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
