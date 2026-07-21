"""
Fetch Homebrew data for (likely) C/C++ formulas:

  1. --step formulas   — formula.json → formulas.csv + dependencies.csv
  2. --step analytics  — yearly install counts via Wayback (install/365d.json)

Run:
    uv run src/sources/homebrew/fetch_homebrew_data.py              # all steps
    uv run src/sources/homebrew/fetch_homebrew_data.py --step formulas
    uv run src/sources/homebrew/fetch_homebrew_data.py --years 2023 2024 2025

Outputs:
    data/sources/homebrew/raw/formulas.csv        name, desc, homepage, source_url, license, tap, language
    data/sources/homebrew/raw/dependencies.csv    formula, dep_name, dep_type, fetched_at
    data/sources/homebrew/raw/downloads.csv       formula, year, downloads
"""

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from rich.console import Console
from rich.table import Table

# ── config ────────────────────────────────────────────────────────────────────

from src.common.freshness import file_is_fresh
from src.common.params import fetch_ttl_days

RAW_FORMULAS = "data/sources/homebrew/raw/formulas.csv"
RAW_DEPS = "data/sources/homebrew/raw/dependencies.csv"
RAW_DOWNLOADS = "data/sources/homebrew/raw/downloads.csv"

# Formula index / analytics move slowly; a warm pipeline run should not
# re-download them. The TTL is 365 days, declared in settings.json;
# --refresh (propagated by the pipeline runner) overrides it.
TTL_DAYS = fetch_ttl_days("sources/homebrew/fetch_homebrew_data")

FORMULA_URL = "https://formulae.brew.sh/api/formula.json"
ANALYTICS_URL = "https://formulae.brew.sh/api/analytics/install/365d.json"
ANALYTICS_FALLBACK = "https://formulae.brew.sh/api/analytics/install-on-request/365d.json"

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_REPLAY = "https://web.archive.org/web/{ts}id_/https://formulae.brew.sh{path}"

YEARS = [2021, 2022, 2023, 2024, 2025]
TIMEOUT = 120
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"

# Pinned snapshot per year. Wayback coverage for formulae.brew.sh/api/analytics
# is sparse and many captures are 1 MB-truncated — we select the best-known
# parseable or partial-parseable snapshot for each target year.
# Snapshot at date D represents installs for the 365d ending at D, so we
# choose a snapshot from Q1-Q4 of the following year as "year D-1" proxy.
YEAR_SNAPSHOT_OVERRIDES: dict[int, str] = {
    2022: "20230509130705",  # 365d ending May 2023 ≈ mid-22 → mid-23, proxy for 2022
    2023: "20230930071333",  # 365d ending Sep 2023, partial-parseable (1 MB-trunc)
    2024: "20250121094300",  # 365d ending Jan 2025, partial-parseable (1 MB-trunc)
    2025: "20251205062935",  # 365d ending Dec 2025, full
}

# Build-dep names that indicate a formula is implemented in a non-C/C++ language.
# Anything matching → language=that; else → language=c/c++ (default assumption,
# since Homebrew's long tail skews heavily toward C/C++ systems libraries).
LANGUAGE_HINTS: list[tuple[str, re.Pattern]] = [
    ("go",      re.compile(r"^go$|^go@")),
    ("rust",    re.compile(r"^rust$|^rust@|^cargo$")),
    ("node",    re.compile(r"^node$|^node@")),
    ("python",  re.compile(r"^python$|^python@")),
    ("ruby",    re.compile(r"^ruby$|^ruby@")),
    ("haskell", re.compile(r"^ghc$|^cabal-install$|^stack$")),
    ("swift",   re.compile(r"^swift$")),
    ("ocaml",   re.compile(r"^ocaml$|^opam$")),
    ("java",    re.compile(r"^openjdk$|^openjdk@|^maven$|^gradle$|^kotlin$")),
    ("dotnet",  re.compile(r"^dotnet$|^mono$")),
    ("erlang",  re.compile(r"^erlang$|^elixir$|^rebar3$")),
    ("crystal", re.compile(r"^crystal$|^shards$")),
    ("zig",     re.compile(r"^zig$")),
    ("scala",   re.compile(r"^scala$|^sbt$")),
    ("lua",     re.compile(r"^lua$|^lua@|^luarocks$")),
    ("r",       re.compile(r"^r$|^r-base$")),
]
# Scripting langs whose presence as a build-dep often just means "scripts
# during build" rather than "the impl language". Downgrade to c/c++ when a
# strong C/C++ build-marker is also present.
SCRIPTING_LANGS = {"python", "ruby", "lua", "r"}
CPP_BUILD_MARKERS = {"cmake", "meson", "ninja", "autoconf", "automake", "libtool", "bison", "flex"}

console = Console()


# ── io helpers ────────────────────────────────────────────────────────────────

def atomic_write(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def parse_int(s: str | None) -> int:
    if not s:
        return 0
    return int(str(s).replace(",", "").strip() or 0)


def guess_language(build_deps: list[str], deps: list[str]) -> str:
    bd = set(build_deps or [])
    rd = set(deps or [])
    has_cpp_marker = bool(bd & CPP_BUILD_MARKERS)

    # Prefer build_deps — impl language is what's needed to build.
    for name, pat in LANGUAGE_HINTS:
        if any(pat.match(d) for d in bd):
            # Scripting-lang build dep + a C/C++ build marker → C/C++ (the
            # scripting lang is just for build tooling, e.g. glib's python
            # with meson/ninja).
            if name in SCRIPTING_LANGS and has_cpp_marker:
                continue
            return name

    # Fallback: runtime deps only if no build_deps matched. Runtime python/
    # ruby deps are usually for bindings or CLI scripts, not impl language,
    # so skip scripting langs here too.
    for name, pat in LANGUAGE_HINTS:
        if name in SCRIPTING_LANGS:
            continue
        if any(pat.match(d) for d in rd):
            return name

    return "c/c++"


# ── step 1: formulas + deps ───────────────────────────────────────────────────

def step_formulas() -> None:
    console.rule("[bold cyan]Step 1 — formulas + dependencies")
    t0 = time.perf_counter()
    r = requests.get(FORMULA_URL, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    console.print(f"  Downloaded {len(r.content) / 1e6:.1f} MB in {time.perf_counter() - t0:.1f}s")

    data = r.json()
    console.print(f"  Parsing {len(data):,} formulas …")

    formula_rows: list[dict] = []
    dep_rows: list[dict] = []
    lang_counts: dict[str, int] = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    for f in data:
        name = f.get("name", "")
        if not name:
            continue
        tap = f.get("tap", "")
        desc = f.get("desc") or ""
        license_ = f.get("license") or ""
        homepage = f.get("homepage") or ""
        source_url = ((f.get("urls") or {}).get("stable") or {}).get("url") or ""

        build_deps = f.get("build_dependencies") or []
        deps = f.get("dependencies") or []
        language = guess_language(build_deps, deps)
        lang_counts[language] = lang_counts.get(language, 0) + 1

        formula_rows.append({
            "name": name, "tap": tap, "desc": desc, "license": license_,
            "homepage": homepage, "source_url": source_url, "language": language,
        })

        emitted = False
        for d in deps:
            dep_rows.append({"formula": name, "dep_name": d, "dep_type": "runtime", "fetched_at": now})
            emitted = True
        for d in build_deps:
            dep_rows.append({"formula": name, "dep_name": d, "dep_type": "build", "fetched_at": now})
            emitted = True
        if not emitted:
            dep_rows.append({"formula": name, "dep_name": "__none__", "dep_type": "", "fetched_at": now})

    formula_rows.sort(key=lambda r: r["name"])
    dep_rows.sort(key=lambda r: (r["formula"], r["dep_type"], r["dep_name"]))
    atomic_write(RAW_FORMULAS, formula_rows,
                 ["name", "tap", "desc", "license", "homepage", "source_url", "language"])
    atomic_write(RAW_DEPS, dep_rows, ["formula", "dep_name", "dep_type", "fetched_at"])

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Formulas",        f"{len(formula_rows):,}")
    tbl.add_row("Dep edges",       f"{sum(1 for r in dep_rows if r['dep_name'] != '__none__'):,}")
    for lang, count in sorted(lang_counts.items(), key=lambda kv: kv[1], reverse=True):
        tbl.add_row(f"  language={lang}", f"{count:,}")
    console.print(tbl)


# ── step 2: analytics via Wayback ─────────────────────────────────────────────

def _http_json_with_retry(url: str, params: dict | None = None) -> list | dict:
    last_exc: Exception | None = None
    for delay in (0, 5, 15, 45):
        if delay:
            time.sleep(delay)
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": USER_AGENT})
            if r.status_code in (429, 502, 503, 504):
                last_exc = requests.HTTPError(f"{r.status_code} {r.reason}")
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            last_exc = e
    raise RuntimeError(f"GET {url} failed after retries: {last_exc}")


def find_wayback_snapshots(path: str, years: list[int]) -> dict[int, list[str]]:
    """For each year, return a list of candidate Wayback timestamps ranked by
    closeness to Dec-31 of that year. The caller tries each in turn — some
    snapshots are truncated by Wayback's 1 MB cache limit and must be skipped.
    A snapshot at date D represents "the 365 days ending at D"; we accept
    [Jun of yr .. Jun of yr+1] as ending inside the year."""
    params = {
        "url": f"formulae.brew.sh{path}",
        "output": "json",
        "filter": ["statuscode:200", "mimetype:application/json"],
        "collapse": "timestamp:8",
    }
    data = _http_json_with_retry(WAYBACK_CDX, params=params) or [[]]
    if len(data) < 2:
        return {}
    ts_idx = data[0].index("timestamp")
    all_ts = [row[ts_idx] for row in data[1:]]

    result: dict[int, list[str]] = {}
    for yr in years:
        override = YEAR_SNAPSHOT_OVERRIDES.get(yr)
        if override:
            # Pinned first, then anything else in-window as backup.
            rest = [ts for ts in all_ts if ts != override]
            result[yr] = [override, *rest]
            continue
        target = int(f"{yr}1231")
        candidates = [
            ts for ts in all_ts
            if int(f"{yr}0601") <= int(ts[:8]) <= int(f"{yr + 1}0630")
        ]
        candidates.sort(key=lambda ts: abs(int(ts[:8]) - target))
        if candidates:
            result[yr] = candidates
    return result


_ITEM_RE = re.compile(
    r'\{"number":\d+,"formula":"([^"]+)","count":"([0-9,]+)","percent":"[^"]+"\}'
)


def _partial_parse(text: str) -> dict[str, int]:
    """Recover complete items from a truncated analytics JSON body.

    Items are ordered by install count descending, so truncation only
    drops the low-install tail. Regex-matches complete `{...}` records
    and ignores whatever got cut mid-token.
    """
    return {m.group(1): int(m.group(2).replace(",", "")) for m in _ITEM_RE.finditer(text)}


def fetch_analytics_snapshot(ts: str, path: str) -> dict[str, int] | None:
    """Return {formula_name: installs} from a Wayback snapshot.

    Wayback caps some cached bodies at exactly 1,048,576 bytes; the body
    is then invalid JSON (cut mid-token). We fall back to regex-based
    partial parsing, which recovers all complete items — enough for the
    high-install head we care about.
    """
    url = WAYBACK_REPLAY.format(ts=ts, path=path)
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    if r.status_code != 200:
        return None
    if len(r.content) == 1_048_576:
        text = r.content.decode("utf-8", errors="ignore")
        recovered = _partial_parse(text)
        return recovered or None
    try:
        data = r.json()
    except (ValueError, json.JSONDecodeError):
        return None
    items = data.get("items") or []
    result: dict[str, int] = {}
    for item in items:
        name = item.get("formula")
        if not name:
            continue
        result[name] = parse_int(item.get("count"))
    return result


def step_analytics(years: list[int], limit: int | None) -> None:
    console.rule("[bold cyan]Step 2 — analytics yearly installs (Wayback)")
    # Prefer install/365d (total installs, dep-inclusive) but fall back to
    # install-on-request/365d (only explicit installs) when needed.
    endpoints = [
        ("/api/analytics/install/365d.json", "install"),
        ("/api/analytics/install-on-request/365d.json", "on-request"),
    ]

    per_endpoint_snaps = {
        path: find_wayback_snapshots(path, years) for path, _ in endpoints
    }

    rows: list[dict] = []
    summary_tbl = Table(show_header=True, box=None, header_style="dim", padding=(0, 2))
    summary_tbl.add_column("Year")
    summary_tbl.add_column("Timestamp")
    summary_tbl.add_column("Endpoint")
    summary_tbl.add_column("Formulas", justify="right")
    summary_tbl.add_column("Max installs", justify="right")

    for yr in years:
        resolved = False
        # For each endpoint, walk candidates closest→farthest until one parses.
        for path, label in endpoints:
            for ts in per_endpoint_snaps.get(path, {}).get(yr, []):
                t0 = time.perf_counter()
                counts = fetch_analytics_snapshot(ts, path)
                elapsed = time.perf_counter() - t0
                if counts is None:
                    console.print(f"  [dim]  skip {yr} {ts} {label} (truncated/invalid, {elapsed:.1f}s)[/dim]")
                    continue
                if limit:
                    counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit])
                for name, installs in counts.items():
                    rows.append({"formula": name, "year": yr, "downloads": installs})
                summary_tbl.add_row(
                    str(yr), ts, label,
                    f"{len(counts):,}",
                    f"{max(counts.values()) if counts else 0:,}",
                )
                resolved = True
                break
            if resolved:
                break
        if not resolved:
            summary_tbl.add_row(str(yr), "[red]— no usable snapshot[/red]", "", "", "")

    console.print(summary_tbl)

    rows.sort(key=lambda r: (r["formula"], r["year"]))
    atomic_write(RAW_DOWNLOADS, rows, ["formula", "year", "downloads"])
    console.print(f"  → wrote {len(rows):,} rows to {RAW_DOWNLOADS}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--step", choices=["formulas", "analytics", "all"], default="all")
    p.add_argument("--years", type=int, nargs="+", default=YEARS)
    p.add_argument("--limit", type=int, default=None,
                   help="Cap per-year analytics rows (keeps top-installs; for testing)")
    p.add_argument("--refresh", action="store_true",
                   help=f"Force refetch, ignoring the {TTL_DAYS}-day output TTL")
    args = p.parse_args()

    console.rule("[bold]homebrew — fetch_homebrew_data")
    console.print(f"  Started : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  Step    : [cyan]{args.step}[/cyan]")
    if args.limit:
        console.print(f"  Limit   : [yellow]{args.limit}[/yellow] (test mode)")
    console.print()

    t0 = time.perf_counter()

    def _skip(label: str, *outputs: str) -> bool:
        """True when this step's outputs are all within TTL."""
        if not args.refresh and all(file_is_fresh(o, TTL_DAYS) for o in outputs):
            console.print(f"  [dim]{label}: outputs fresh (< {TTL_DAYS}d) — skipping; --refresh to force[/dim]")
            return True
        return False

    if args.step in ("formulas", "all") and not _skip("formulas", RAW_FORMULAS, RAW_DEPS):
        step_formulas()
    if args.step in ("analytics", "all") and not _skip("analytics", RAW_DOWNLOADS):
        step_analytics(args.years, args.limit)
    console.print(f"\n[bold green]Done in {time.perf_counter() - t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
