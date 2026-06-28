#!/usr/bin/env python3
"""Check EOL for the unified C/C++ ecosystem (Debian + Homebrew).

Two signal sources, applied in priority order per cpp package:

1. **Homebrew formula flags** (`homebrew_disabled`, `homebrew_deprecated`)
   The `disable!` and `deprecate!` DSL stanzas in formula Ruby files,
   exposed by the Homebrew JSON API. Maintainer-set, per-formula.
   Coverage: ~39% of cpp packages have a Homebrew formula via Repology.

2. **endoflife.date cycle EOL** (`endoflife_date`)
   For a curated set of well-known products (openssl, postgresql, python,
   ruby, php, …), the project is EOL if every published release cycle has
   an `eol` date in the past. Small but very reliable.

Debian "removed from current stable" was considered and rejected — it has
high false-positive rate (SONAME bumps, python2→3 transitions, RC-bug
removals, FTBFS, package renames). See docs/eligibility.md for the
analysis. A future signal could parse `ftp-master.debian.org/removals.txt`
and filter for `Reason: RoQA; Dead upstream`, but that's deferred.

Reads:
    data/sources/cpp/results.csv               — cpp packages we care about
    data/sources/repology/packages.csv         — cpp project ↔ homebrew formula
    data/sources/homebrew/raw/formula-api.json — cached Homebrew API (auto-fetched)
    data/sources/endoflife/<product>.json      — cached endoflife.date per product

Writes:
    data/sources/cpp/eol.csv

Usage:
    uv run python -m src.sources.cpp.check_eol
    uv run python -m src.sources.cpp.check_eol --refresh   # refetch caches
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import date, datetime
from pathlib import Path

import requests
from rich.console import Console

from src.common.eol_common import display_summary, now_iso, write_eol

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
RESULTS_FILE = DATA_DIR / "sources" / "cpp" / "results.csv"
REPOLOGY_FILE = DATA_DIR / "sources" / "repology" / "packages.csv"
OUTPUT_FILE = DATA_DIR / "sources" / "cpp" / "eol.csv"
HOMEBREW_CACHE = DATA_DIR / "sources" / "homebrew" / "raw" / "formula-api.json"
ENDOFLIFE_DIR = DATA_DIR / "sources" / "endoflife"

HOMEBREW_API = "https://formulae.brew.sh/api/formula.json"
ENDOFLIFE_API = "https://endoflife.date/api/{product}.json"

# Curated cpp_package → endoflife.date product slug.
# Only entries verified to exist on endoflife.date (`/api/all.json`). bash,
# boost, git, jq, openssh, samba aren't tracked there.
ENDOFLIFE_MAP: dict[str, str] = {
    "etcd": "etcd",
    "libreoffice": "libreoffice",
    "lua": "lua",
    "mariadb": "mariadb",
    "memcached": "memcached",
    "mongodb": "mongodb",
    "mysql": "mysql",
    "nginx": "nginx",
    "openssl": "openssl",
    "perl": "perl",
    "php": "php",
    "postgresql": "postgresql",
    "python": "python",
    "qt": "qt",
    "rabbitmq": "rabbitmq",
    "redis": "redis",
    "ruby": "ruby",
    "sqlite": "sqlite",
    "tomcat": "tomcat",
    "varnish": "varnish",
}


# ---------- Homebrew ----------

def fetch_homebrew_api(refresh: bool) -> dict[str, dict]:
    """Return {formula_name: formula_dict}, fetching once and caching."""
    if not refresh and HOMEBREW_CACHE.exists():
        log.info("loading cached homebrew api from %s", HOMEBREW_CACHE)
    else:
        HOMEBREW_CACHE.parent.mkdir(parents=True, exist_ok=True)
        log.info("fetching %s", HOMEBREW_API)
        r = requests.get(HOMEBREW_API, timeout=60)
        r.raise_for_status()
        HOMEBREW_CACHE.write_bytes(r.content)
        log.info("cached %.1f MB → %s", len(r.content) / 1e6, HOMEBREW_CACHE)
    raw = json.loads(HOMEBREW_CACHE.read_bytes())
    return {f["name"]: f for f in raw}


def homebrew_eol(formulas: list[str], brew_data: dict[str, dict]) -> tuple[bool, str, str]:
    """Return (is_eol, eol_method, reason) for a project given its Homebrew formulas.

    A project is EOL via Homebrew iff every mapped formula is `disabled` or
    `deprecated`. If any formula is alive, the project is alive. `disabled`
    takes priority over `deprecated` when reporting the method.
    """
    known = [f for f in formulas if f in brew_data]
    if not known:
        return False, "", ""
    statuses = []
    reasons: list[str] = []
    for f in known:
        d = brew_data[f]
        if d.get("disabled"):
            statuses.append("disabled")
            r = d.get("disable_reason") or "disabled"
            reasons.append(f"{f}: {r}")
        elif d.get("deprecated"):
            statuses.append("deprecated")
            r = d.get("deprecation_reason") or "deprecated"
            reasons.append(f"{f}: {r}")
        else:
            statuses.append("alive")
    if "alive" in statuses:
        return False, "", ""
    method = "homebrew_disabled" if "disabled" in statuses else "homebrew_deprecated"
    return True, method, "; ".join(reasons)[:500]


# ---------- endoflife.date ----------

def fetch_endoflife(product: str, refresh: bool) -> list[dict] | None:
    """Return endoflife.date cycles for `product`, caching per file."""
    ENDOFLIFE_DIR.mkdir(parents=True, exist_ok=True)
    cache = ENDOFLIFE_DIR / f"{product}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_bytes())
    url = ENDOFLIFE_API.format(product=product)
    r = requests.get(url, timeout=20)
    if r.status_code == 404:
        log.warning("endoflife.date 404 for %s", product)
        return None
    r.raise_for_status()
    cache.write_bytes(r.content)
    return r.json()


def endoflife_eol(cycles: list[dict], today: date) -> tuple[bool, str]:
    """Project-level EOL via endoflife.date.

    Returns (is_eol, reason). EOL iff the **maximum** `eol` date across all
    cycles is in the past — we model project-level EOL, not version-level,
    so it's enough that any one cycle still has support life left to keep
    the project alive.

    A cycle with `eol: false` keeps the project alive (vendor declared an
    open-ended supported cycle). `eol: true` and other non-date values are
    ignored.
    """
    max_eol: date | None = None
    for c in cycles:
        eol = c.get("eol")
        if eol is False:
            return False, "at least one cycle has eol=false"
        if not isinstance(eol, str):
            continue
        try:
            d = date.fromisoformat(eol)
        except ValueError:
            continue
        if max_eol is None or d > max_eol:
            max_eol = d
    if max_eol is None:
        return False, "no parseable eol dates"
    if max_eol >= today:
        return False, f"max eol={max_eol.isoformat()}"
    return True, f"max eol={max_eol.isoformat()} < today"


# ---------- mapping ----------

def load_homebrew_formula_map() -> dict[str, list[str]]:
    """Build {cpp_project: [homebrew_formula_name, ...]} from repology."""
    out: dict[str, list[str]] = {}
    with open(REPOLOGY_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["repo"] != "homebrew":
                continue
            name = r["visiblename"] or r["srcname"]
            if not name:
                continue
            out.setdefault(r["project"], []).append(name)
    return out


def load_packages(limit: int | None) -> list[str]:
    pkgs: list[str] = []
    with open(RESULTS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pkgs.append(r["package"])
            if limit and len(pkgs) >= limit:
                break
    return pkgs


# ---------- main ----------

def build_rows(packages: list[str], refresh: bool) -> list[dict]:
    brew_data = fetch_homebrew_api(refresh)
    formula_map = load_homebrew_formula_map()
    log.info("homebrew formulas in api: %d  | cpp projects with homebrew formula: %d",
             len(brew_data), len(formula_map))

    # Pre-fetch all endoflife.date products we care about
    eol_products: dict[str, list[dict] | None] = {}
    for slug in sorted(set(ENDOFLIFE_MAP.values())):
        eol_products[slug] = fetch_endoflife(slug, refresh)
    log.info("endoflife.date products fetched: %d", sum(1 for v in eol_products.values() if v))

    today = date.today()
    checked_at = now_iso()
    rows: list[dict] = []

    for pkg in packages:
        # 1. Homebrew signal
        formulas = formula_map.get(pkg, [])
        is_eol, method, reason = homebrew_eol(formulas, brew_data)
        if is_eol:
            rows.append(_row(pkg, True, method, reason, "registry", checked_at))
            continue

        # 2. endoflife.date signal
        eol_slug = ENDOFLIFE_MAP.get(pkg.lower())
        if eol_slug:
            cycles = eol_products.get(eol_slug)
            if cycles is not None:
                is_eol, reason = endoflife_eol(cycles, today)
                if is_eol:
                    rows.append(_row(pkg, True, "endoflife_date",
                                     f"{eol_slug}: {reason}", "registry", checked_at))
                    continue

        # 3. No signal — alive (or unknown). Tag method as the strongest source we tried.
        if formulas:
            method = "homebrew_alive"
            source = "registry"
        elif eol_slug:
            method = "endoflife_alive"
            source = "registry"
        else:
            method = "unsupported"
            source = "unsupported"
        rows.append(_row(pkg, False, method, "", source, checked_at))

    return rows


def _row(package: str, is_eol: bool, method: str, reason: str,
         source: str, checked_at: str) -> dict:
    return {
        "package": package,
        "is_eol": is_eol,
        "eol_method": method,
        "eol_reason": reason,
        "source": source,
        "eol_checked_at": checked_at,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--refresh", action="store_true",
                   help="refetch Homebrew API + endoflife.date caches")
    args = p.parse_args()

    started = datetime.now()
    console.print(f"[bold]cpp.check_eol[/bold]  limit={args.limit or '∞'}  "
                  f"refresh={args.refresh}  started={started:%Y-%m-%d %H:%M:%S}")

    pkgs = load_packages(args.limit)
    log.info("loaded %d cpp packages from results.csv", len(pkgs))

    rows = build_rows(pkgs, args.refresh)
    write_eol(OUTPUT_FILE, rows)

    display_summary(console, "cpp", rows)
    console.print(f"\n[green]wrote[/green] {OUTPUT_FILE.relative_to(DATA_DIR.parent)}  "
                  f"({len(rows)} rows, {(datetime.now() - started).total_seconds():.1f}s)")


if __name__ == "__main__":
    main()
