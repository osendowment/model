"""Second-pass license resolver for repos GitHub flagged NOASSERTION / empty.

GitHub's license detector (Licensee) refuses to commit to an SPDX ID
unless the LICENSE file matches one of its templates with ≥95% similarity.
That bar is too strict for the typical OSS repo:

  • numpy concatenates a BSD-3-Clause LICENSE with bundled-library notices.
  • lodash prepends a multi-paragraph attribution block before the MIT text.
  • matplotlib stores licenses under a `LICENSE/` directory rather than at root.

All three are unambiguously OSS but show up as `NOASSERTION` (or 404 from
the /license endpoint). This script does the same thing a human would:

  1. For every repo in `data/sources/github/repos.csv` whose `license` is empty or
     `NOASSERTION`, fetch `/repos/{slug}/license`.
  2. If the API still says NOASSERTION, decode the file content and run a
     keyword/SPDX pattern classifier to pick the most likely SPDX ID.
  3. Write the resolved value back to `repos.csv`'s `license` column and
     record provenance in a new `license_source` column:
       - `github`           — the original GitHub detection was confident
       - `content_pattern`  — we classified by reading the LICENSE file
       - `unknown`          — couldn't classify, license stays as ""

Usage:
    uv run python -m src.github.resolve_licenses
    uv run python -m src.github.resolve_licenses --force   # ignore TTL
    uv run python -m src.github.resolve_licenses --limit 20
"""

import argparse
import asyncio
import base64
import csv
import datetime as dt
import logging
import os
import re
import time
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           TextColumn, TimeElapsedColumn)
from rich.table import Table

from src.github.fetch_repo_owner_data import (REPO_FIELDS, REPOS_OUT,
                                              _Limiter, _now_iso)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

console = Console()

GITHUB_API = "https://api.github.com"
TTL_DAYS = 90
MAX_CONCURRENT = 10

# Schema additions: append to whatever's already in REPO_FIELDS so old
# files keep loading. We mutate the imported list in place because every
# downstream writer (`fetch_repo_owner_data.upsert`) uses the same global.
for col in ("license_source", "license_checked_at"):
    if col not in REPO_FIELDS:
        REPO_FIELDS.append(col)


# ── Content-pattern classifier ────────────────────────────────────────────────
#
# Order matters. More specific patterns come first so e.g. AGPL doesn't
# get caught by the generic GPL matcher. Each pattern returns the
# canonical SPDX-cased ID so the value lands in `eligibility-data.csv`
# in the form a downstream consumer expects.

_LICENSE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # SPDX expression header — strongest possible signal
    ("__SPDX__", re.compile(r"SPDX-License-Identifier:\s*([\w.+-]+)", re.I)),

    # Apache 2.0 — match either the long-form title or its preamble
    ("Apache-2.0", re.compile(
        r"apache\s+license[, ]+\s*version\s+2\.0|"
        r"licensed under the apache license,\s*version\s+2\.0", re.I)),

    # MIT — must contain the unmistakable permission grant
    ("MIT", re.compile(
        r"permission is hereby granted,\s+free of charge,?\s+to any person\s+"
        r"obtaining a copy", re.I | re.S)),

    # ISC — same permission grant as MIT but with "as is" wording
    ("ISC", re.compile(
        r"permission to use,\s+copy,\s+modify,?\s+and(?:/or)?\s+distribute "
        r"this software", re.I | re.S)),

    # BSD-3-Clause — the "Neither the name of … may be used to endorse" clause
    ("BSD-3-Clause", re.compile(
        r"neither the names? of (?:the [^\n]*?|its contributors)[^\n]*"
        r"may be used to endorse or promote", re.I | re.S)),

    # BSD-2-Clause — has redistribution clauses but no third "endorse" clause
    ("BSD-2-Clause", re.compile(
        r"redistributions in binary form must reproduce the above copyright\s+"
        r"notice", re.I | re.S)),

    # 0BSD — explicit
    ("0BSD", re.compile(r"\bbsd zero[- ]clause license\b|\b0bsd license\b", re.I)),

    # MPL 2.0
    ("MPL-2.0", re.compile(
        r"mozilla public license,\s*v\.?\s*2\.0|mpl-?2\.0", re.I)),

    # GNU family — order matters (LGPL/AGPL before GPL)
    ("AGPL-3.0", re.compile(
        r"gnu affero general public license\s+version 3", re.I)),
    ("LGPL-3.0", re.compile(
        r"gnu lesser general public license\s+version 3", re.I)),
    ("LGPL-2.1", re.compile(
        r"gnu lesser general public license\s+version 2\.1", re.I)),
    ("GPL-3.0", re.compile(
        r"gnu general public license\s+version 3", re.I)),
    ("GPL-2.0", re.compile(
        r"gnu general public license[\s,]+version 2", re.I)),

    # Eclipse / Boost / Zlib / Unlicense / CC0
    ("EPL-2.0", re.compile(r"eclipse public license\s*-?\s*v(?:ersion)?\s*2", re.I)),
    ("EPL-1.0", re.compile(r"eclipse public license\s*-?\s*v(?:ersion)?\s*1", re.I)),
    ("BSL-1.0", re.compile(r"boost software license\s*-?\s*version\s*1", re.I)),
    ("Zlib", re.compile(r"\bzlib license\b", re.I)),
    ("Unlicense", re.compile(r"\bthis is free and unencumbered software\b", re.I)),
    ("CC0-1.0", re.compile(r"creative commons (?:cc0|zero)\s*1\.0", re.I)),

    # PostgreSQL, NCSA, BlueOak, OFL — long-tail OSI-approved
    ("PostgreSQL", re.compile(r"postgresql.*?(?:license|licence)", re.I)),
    ("NCSA", re.compile(
        r"university of illinois/ncsa open source license", re.I)),
    ("BlueOak-1.0.0", re.compile(r"blue oak model license", re.I)),
    ("OFL-1.1", re.compile(r"sil open font license,\s*version\s*1\.1", re.I)),

    # Creative Commons BY (NOT OSS — but we still want to record the SPDX)
    ("CC-BY-4.0", re.compile(
        r"creative commons attribution\s*4\.0", re.I)),
]

# Whitelist of SPDX IDs we'll accept from the SPDX-Identifier header. Any
# value outside this set falls through to the keyword classifier so we
# don't blindly trust e.g. typos.
_KNOWN_SPDX = {p[0].lower() for p in _LICENSE_PATTERNS if p[0] != "__SPDX__"}


def classify_license_text(text: str) -> str:
    """Return the canonical SPDX ID for the given LICENSE file content, or ""."""
    if not text:
        return ""
    # Truncate aggressively — anything past 50 KB is bundled-license noise.
    text = text[:50_000]

    # 1) Explicit SPDX-License-Identifier header (highest signal)
    for spdx_id, pat in _LICENSE_PATTERNS:
        if spdx_id != "__SPDX__":
            continue
        m = pat.search(text)
        if m:
            tag = m.group(1).strip()
            # Trust only known IDs; fall through otherwise.
            if tag.lower() in _KNOWN_SPDX:
                # Map back to canonical casing
                for canon, _ in _LICENSE_PATTERNS:
                    if canon != "__SPDX__" and canon.lower() == tag.lower():
                        return canon

    # 2) Body keyword patterns
    for spdx_id, pat in _LICENSE_PATTERNS:
        if spdx_id == "__SPDX__":
            continue
        if pat.search(text):
            return spdx_id

    return ""


# ── /license endpoint fetch ────────────────────────────────────────────────


async def _fetch_license(limiter: _Limiter, session: aiohttp.ClientSession,
                         slug: str) -> tuple[str, str, str]:
    """Return (slug, resolved_spdx, source).

    source ∈ {"github", "content_pattern", "unknown"}.
    Empty string for resolved_spdx only when source == "unknown".
    """
    url = f"{GITHUB_API}/repos/{slug}/license"
    for attempt in range(3):
        try:
            resp = await limiter.get(session, url)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2 ** attempt)
            continue
        async with resp:
            if resp.status == 404:
                # No LICENSE file in the conventional location — happens with
                # matplotlib (LICENSE/ subdir), some C/C++ projects, etc.
                return slug, "", "unknown"
            if resp.status == 403:
                await asyncio.sleep(2)
                continue
            if resp.status != 200:
                return slug, "", "unknown"
            data = await resp.json()
            api_spdx = ((data.get("license") or {}).get("spdx_id") or "").strip()
            if api_spdx and api_spdx.upper() != "NOASSERTION":
                return slug, api_spdx, "github"
            content_b64 = data.get("content") or ""
            try:
                text = base64.b64decode(content_b64).decode(
                    "utf-8", errors="replace")
            except Exception:
                text = ""
            spdx = classify_license_text(text)
            if spdx:
                return slug, spdx, "content_pattern"
            return slug, "", "unknown"
    return slug, "", "unknown"


# ── Orchestration ──────────────────────────────────────────────────────────


def _ttl_cutoff() -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(days=TTL_DAYS)


def _is_fresh(when: str, cutoff: dt.datetime) -> bool:
    if not when:
        return False
    try:
        ts = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        return ts >= cutoff
    except Exception:
        return False


def _needs_resolution(row: dict, cutoff: dt.datetime, force: bool) -> bool:
    if row.get("valid", "").lower() != "true":
        return False  # 404'd repo, no LICENSE to fetch
    lic = (row.get("license") or "").strip().upper()
    if lic and lic != "NOASSERTION":
        return False  # already confidently classified
    if not force and _is_fresh(row.get("license_checked_at", ""), cutoff):
        return False
    return True


async def _run(rows_to_check: list[dict], all_rows: dict[str, dict]
               ) -> dict[str, int]:
    limiter = _Limiter()
    counts: dict[str, int] = {"github": 0, "content_pattern": 0,
                              "unknown": 0, "error": 0}
    progress = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[status]}"),
        console=console,
    )
    async with aiohttp.ClientSession() as session:
        with progress:
            task = progress.add_task("resolving", total=len(rows_to_check),
                                     status="")
            sem = asyncio.Semaphore(MAX_CONCURRENT)

            async def _wrap(row):
                async with sem:
                    return await _fetch_license(limiter, session, row["repo"])

            coros = [asyncio.create_task(_wrap(r)) for r in rows_to_check]
            for coro in asyncio.as_completed(coros):
                slug, spdx, source = await coro
                row = all_rows[slug.lower()]
                if spdx:
                    row["license"] = spdx
                row["license_source"] = source
                row["license_checked_at"] = _now_iso()
                counts[source] = counts.get(source, 0) + 1
                progress.update(task, advance=1,
                                status=f"{limiter.n} api calls · "
                                f"unknown={counts['unknown']}")
    return counts


def _atomic_write(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REPO_FIELDS,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["repo"].lower()):
            w.writerow(r)
    os.replace(tmp, path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--force", action="store_true",
                   help="ignore TTL on `license_checked_at`")
    p.add_argument("--limit", type=int, default=None,
                   help="cap on rows to resolve (testing)")
    args = p.parse_args()

    if not REPOS_OUT.exists():
        raise SystemExit(f"missing {REPOS_OUT} — run fetch_repo_owner_data first")

    console.rule("[bold cyan]github/resolve_licenses")
    console.print(f"  TTL: [dim]{TTL_DAYS} days[/dim]   "
                  f"force=[dim]{args.force}[/dim]")

    # Load every row, decide which need resolution.
    with open(REPOS_OUT, encoding="utf-8") as f:
        all_rows_list = list(csv.DictReader(f))
    all_rows = {r["repo"].lower(): r for r in all_rows_list}
    cutoff = _ttl_cutoff()
    targets = [r for r in all_rows_list if _needs_resolution(r, cutoff, args.force)]
    if args.limit:
        targets = targets[: args.limit]

    total = len(all_rows_list)
    confident = sum(1 for r in all_rows_list
                    if (r.get("license") or "").strip().upper() not in
                    ("", "NOASSERTION"))
    console.print(f"  Total repos:               [bold]{total:,}[/bold]")
    console.print(f"  Already confident:         [green]{confident:,}[/green]")
    console.print(f"  To resolve:                [yellow]{len(targets):,}[/yellow]\n")

    if not targets:
        console.print("[dim]Nothing to do.[/dim]")
        return

    t0 = time.monotonic()
    counts = asyncio.run(_run(targets, all_rows))
    elapsed = time.monotonic() - t0

    _atomic_write(REPOS_OUT, list(all_rows.values()))

    tbl = Table(title="[bold]Resolution outcome[/bold]", header_style="bold dim")
    tbl.add_column("source"); tbl.add_column("count", justify="right")
    tbl.add_column("%", justify="right")
    for src, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        pct = 100 * n / max(len(targets), 1)
        tbl.add_row(src, f"{n:,}", f"{pct:.1f}%")
    tbl.add_section()
    tbl.add_row("[bold]Total[/bold]", f"[bold]{len(targets):,}[/bold]", "")
    console.print()
    console.print(tbl)
    console.print(f"\n  [dim]elapsed {elapsed:.1f}s · "
                  f"updated → [cyan]{REPOS_OUT}[/cyan][/dim]")


if __name__ == "__main__":
    main()
