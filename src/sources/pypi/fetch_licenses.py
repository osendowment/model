"""Fetch SPDX licenses for every package in `data/sources/pypi/results.csv`.

Two-stage flow so the data survives `process_data.py` re-runs:

  1. Fetch → `data/sources/pypi/raw/licenses.csv` (persistent cache, 90-day TTL).
     Schema: package, license, fetched_at.
  2. Apply → joins the raw cache into `data/sources/pypi/results.csv` as a
     `license` column. Re-runs after `process_data.py` rewrites
     `results.csv` cost zero API calls — the apply step pulls from raw.

PyPI exposes three license signals per package; we use them in this order
of preference (first hit wins):

  1. `info.license_expression` — PEP 639 SPDX, set on modern uploads.
                                  Already an SPDX string — just lower-case.
  2. `info.classifiers[*]`     — `License :: OSI Approved :: <Name>`.
                                  Mapped through a small table to the
                                  canonical SPDX ID (`mit`, `apache-2.0`).
  3. `info.license`            — free-form text. Only used when nothing
                                  else worked, and only when it parses
                                  cleanly as an SPDX-looking token.

Output is **lowercase SPDX**.

Async with 80-way concurrency.

Usage:
    uv run python -m src.sources.pypi.fetch_licenses                # cached + apply
    uv run python -m src.sources.pypi.fetch_licenses --force        # ignore TTL
    uv run python -m src.sources.pypi.fetch_licenses --apply-only   # skip API, just join cache → results.csv
    uv run python -m src.sources.pypi.fetch_licenses --limit 100
    uv run python -m src.sources.pypi.fetch_licenses --concurrency 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import logging
import os
import re
from collections import Counter
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.table import Table
from tqdm.asyncio import tqdm_asyncio

logging.basicConfig(level="WARNING")
log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
RESULTS = DATA_DIR / "sources" / "pypi" / "results.csv"
RAW = DATA_DIR / "sources" / "pypi" / "raw" / "licenses.csv"

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"
TTL_DAYS = 90
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
        w = csv.DictWriter(f, fieldnames=RAW_FIELDS,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        for pkg in sorted(cache.keys()):
            w.writerow(cache[pkg])
    os.replace(tmp, RAW)

# Trove "License :: OSI Approved :: <X>" → SPDX. Covers everything PyPI
# routinely emits. Anything not in this map silently falls through.
_TROVE_TO_SPDX: dict[str, str] = {
    "academic free license (afl)": "afl-3.0",
    "apache software license": "apache-2.0",
    "apple public source license": "apsl-2.0",
    "artistic license": "artistic-2.0",
    "boost software license 1.0 (bsl-1.0)": "bsl-1.0",
    "bsd license": "bsd-3-clause",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "cc0-1.0",
    "common development and distribution license 1.0 (cddl-1.0)": "cddl-1.0",
    "common public license": "cpl-1.0",
    "eclipse public license 1.0 (epl-1.0)": "epl-1.0",
    "eclipse public license 2.0 (epl-2.0)": "epl-2.0",
    "european union public licence 1.1 (eupl 1.1)": "eupl-1.1",
    "european union public licence 1.2 (eupl 1.2)": "eupl-1.2",
    "gnu affero general public license v3": "agpl-3.0",
    "gnu affero general public license v3 or later (agplv3+)": "agpl-3.0-or-later",
    "gnu affero general public license v3 or later (agplv3+)": "agpl-3.0-or-later",
    "gnu free documentation license (fdl)": "gfdl-1.3",
    "gnu general public license (gpl)": "gpl-2.0",
    "gnu general public license v2 (gplv2)": "gpl-2.0",
    "gnu general public license v2 or later (gplv2+)": "gpl-2.0-or-later",
    "gnu general public license v3 (gplv3)": "gpl-3.0",
    "gnu general public license v3 or later (gplv3+)": "gpl-3.0-or-later",
    "gnu lesser general public license v2 (lgplv2)": "lgpl-2.1",
    "gnu lesser general public license v2 or later (lgplv2+)": "lgpl-2.1-or-later",
    "gnu lesser general public license v3 (lgplv3)": "lgpl-3.0",
    "gnu lesser general public license v3 or later (lgplv3+)": "lgpl-3.0-or-later",
    "gnu library or lesser general public license (lgpl)": "lgpl-2.1",
    "isc license (iscl)": "isc",
    "mit license": "mit",
    "mit no attribution license (mit-0)": "mit-0",
    "mozilla public license 1.0 (mpl)": "mpl-1.0",
    "mozilla public license 1.1 (mpl 1.1)": "mpl-1.1",
    "mozilla public license 2.0 (mpl 2.0)": "mpl-2.0",
    "open software license 3.0 (osl-3.0)": "osl-3.0",
    # SPDX-recognised id is `python-2.0` (PSF License v2). The legacy
    # `psf-2.0` form is not in SPDX and not on the OSI list.
    "python software foundation license": "python-2.0",
    "qt public license (qpl)": "qpl-1.0",
    "sil open font license 1.1 (ofl-1.1)": "ofl-1.1",
    "the unlicense (unlicense)": "unlicense",
    "universal permissive license (upl)": "upl-1.0",
    "vovida software license 1.0": "vsl-1.0",
    "w3c license": "w3c",
    "zope public license": "zpl-2.1",
    "zlib/libpng license": "zlib",
}

# Tokens we'll accept from `info.license` if no better source. Conservative
# on purpose — free-text license fields are noisy.
_LICENSE_TEXT_RX = re.compile(
    r"^\s*(mit|apache-?2\.?0|bsd-?[23]?-?clause|isc|gpl-?[23](?:\.0)?(?:-or-later)?|"
    r"lgpl-?[23](?:\.[01])?(?:-or-later)?|agpl-?3(?:\.0)?(?:-or-later)?|"
    r"mpl-?2\.0|epl-?[12]\.0|cc0-?1\.0|unlicense|wtfpl|psf|"
    r"blueoak-?1\.0\.0)\s*$",
    re.I,
)


def _from_classifiers(classifiers: list[str]) -> str:
    for c in classifiers or []:
        # `License :: OSI Approved :: <X>`  or  `License :: <X>`
        parts = [p.strip() for p in c.split("::")]
        if len(parts) >= 2 and parts[0].lower() == "license":
            label = parts[-1].strip().lower()
            spdx = _TROVE_TO_SPDX.get(label)
            if spdx:
                return spdx
    return ""


def _from_freeform(text: str) -> str:
    if not text:
        return ""
    # First line only, capped — many projects paste the whole license here.
    first_line = text.strip().splitlines()[0][:80] if text.strip() else ""
    m = _LICENSE_TEXT_RX.match(first_line)
    if m:
        return m.group(1).lower().replace(" ", "")
    return ""


def _resolve(info: dict) -> str:
    spdx = (info.get("license_expression") or "").strip().lower()
    if spdx:
        return spdx
    spdx = _from_classifiers(info.get("classifiers") or [])
    if spdx:
        return spdx
    return _from_freeform(info.get("license") or "")


async def _fetch(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                 pkg: str) -> tuple[str, str]:
    url = PYPI_JSON.format(name=pkg)
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
                    return pkg, _resolve(data.get("info") or {})
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(0.5 * (attempt + 1))
        return pkg, ""


async def _collect(packages: list[str], concurrency: int) -> dict[str, str]:
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, str] = {}
    async with aiohttp.ClientSession() as session:
        coros = [_fetch(session, sem, p) for p in packages]
        for fut in tqdm_asyncio.as_completed(
            coros, total=len(coros), desc="pypi licenses"
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
                   help="ignore 90-day TTL, refetch everything")
    p.add_argument("--apply-only", action="store_true",
                   help="skip the API fetch — only join the existing raw cache into results.csv")
    args = p.parse_args()

    console.rule("[bold cyan]pypi/fetch_licenses")
    console.print(f"  raw cache: [dim]{RAW}[/dim]   TTL=[dim]{TTL_DAYS}d[/dim]")

    if not RESULTS.exists():
        raise SystemExit(f"missing {RESULTS}")

    cache = _load_raw_cache()
    console.print(f"  loaded [bold]{len(cache):,}[/bold] cached license rows from raw")

    if not args.apply_only:
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
