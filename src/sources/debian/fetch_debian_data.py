"""
Fetch Debian data for C/C++ packages:

  1. --step packages  — list of C/C++ packages (UDD debtags: implemented-in::c[++])
  2. --step popcon    — yearly install counts (popcon snapshots via Wayback CDX)
  3. --step index     — latest Packages.xz → dependencies + Homepage/Vcs-Browser

Run:
    uv run src/sources/debian/fetch_debian_data.py              # all steps
    uv run src/sources/debian/fetch_debian_data.py --step popcon
    uv run src/sources/debian/fetch_debian_data.py --years 2023 2024 2025
    uv run src/sources/debian/fetch_debian_data.py --limit 100  # test mode

Outputs:
    data/sources/debian/raw/cpp-packages.csv       package, tag
    data/sources/debian/raw/downloads.csv          package, year, downloads
    data/sources/debian/raw/dependencies.csv       package, dep_name, dep_version, fetched_at
    data/sources/debian/raw/package-metadata.csv   package, homepage, vcs_browser, section
"""

import argparse
import csv
import gzip
import io
import lzma
import os
import re
import time
from datetime import datetime, timezone

import psycopg
import requests
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

# ── config ────────────────────────────────────────────────────────────────────

from src.common.freshness import file_is_fresh

RAW_PACKAGES = "data/sources/debian/raw/cpp-packages.csv"
RAW_DOWNLOADS = "data/sources/debian/raw/downloads.csv"
RAW_DEPS = "data/sources/debian/raw/dependencies.csv"
RAW_METADATA = "data/sources/debian/raw/package-metadata.csv"
RAW_ALIASES = "data/sources/debian/raw/aliases.csv"

# Registry indexes / popcon stats move slowly; a warm pipeline run should not
# re-download them. --refresh (propagated by the pipeline runner) overrides.
TTL_DAYS = 7

UDD_DSN = "host=udd-mirror.debian.net port=5432 user=udd-mirror password=udd-mirror dbname=udd"

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_REPLAY = "https://web.archive.org/web/{ts}id_/https://popcon.debian.org/by_inst.gz"
POPCON_URL = "popcon.debian.org/by_inst.gz"

PACKAGES_URL = "https://deb.debian.org/debian/dists/stable/main/binary-amd64/Packages.xz"

YEARS = [2021, 2022, 2023, 2024, 2025]
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"
TIMEOUT = 120

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


# ── step 1: C/C++ package list via UDD ────────────────────────────────────────
#
# debtags covers some but not all C/C++ packages. We union three signals:
#   direct    — binary pkg explicitly tagged implemented-in::c[++]
#   source    — binary pkg whose SOURCE pkg is tagged (catches e.g. libssl3t64 ← openssl)
#   section   — binary pkg in Section ∈ {libs, libdevel, oldlibs} in current stable
#               (Debian convention reserves these sections for C/C++ libraries;
#                other langs use their own section: python, perl, haskell, …)
#
# Precedence when assigning `tag`: direct > source > section.
# `section`-only matches default to implemented-in::c (safe — C is the majority).

_PACKAGES_QUERY = """
    WITH direct AS (
        SELECT package, MIN(tag) AS tag, 'direct' AS via
        FROM debtags
        WHERE tag IN ('implemented-in::c', 'implemented-in::c++')
        GROUP BY package
    ),
    via_source AS (
        SELECT DISTINCT ON (p.package)
               p.package, d.tag, 'source:' || p.source AS via
        FROM packages p
        JOIN debtags d ON d.package = p.source
        WHERE d.tag IN ('implemented-in::c', 'implemented-in::c++')
        ORDER BY p.package, d.tag   -- prefer implemented-in::c (alphabetical) for determinism
    ),
    via_section AS (
        SELECT DISTINCT package, 'implemented-in::c' AS tag, 'section:' || section AS via
        FROM packages
        WHERE release = 'trixie' AND component = 'main'
          AND section IN ('libs', 'libdevel', 'oldlibs')
    )
    SELECT package, tag, via FROM direct
    UNION ALL
    SELECT package, tag, via FROM via_source
      WHERE package NOT IN (SELECT package FROM direct)
    UNION ALL
    SELECT package, tag, via FROM via_section
      WHERE package NOT IN (SELECT package FROM direct)
        AND package NOT IN (SELECT package FROM via_source)
    ORDER BY package
"""


def step_packages() -> set[str]:
    console.rule("[bold cyan]Step 1 — C/C++ package list (UDD: debtags direct + source + Section)")
    t0 = time.perf_counter()
    with psycopg.connect(UDD_DSN, connect_timeout=15) as conn:
        rows = conn.execute(_PACKAGES_QUERY).fetchall()

    out: list[dict] = []
    via_counts: dict[str, int] = {"direct": 0, "source": 0, "section": 0}
    tag_counts: dict[str, int] = {"implemented-in::c": 0, "implemented-in::c++": 0}
    for package, tag, via in rows:
        pkg = package.decode() if isinstance(package, bytes) else package
        t = tag.decode() if isinstance(tag, bytes) else tag
        v = via.decode() if isinstance(via, bytes) else via
        out.append({"package": pkg, "tag": t, "via": v})
        tag_counts[t] += 1
        via_counts[v.split(":")[0]] += 1

    atomic_write(RAW_PACKAGES, out, ["package", "tag", "via"])

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("via direct (debtags)",     f"{via_counts['direct']:,}")
    tbl.add_row("via source (debtags)",     f"{via_counts['source']:,}")
    tbl.add_row("via section (libs/…)",     f"{via_counts['section']:,}")
    tbl.add_row("C tagged",                 f"{tag_counts['implemented-in::c']:,}")
    tbl.add_row("C++ tagged",               f"{tag_counts['implemented-in::c++']:,}")
    tbl.add_row("Total unique packages",    f"{len(out):,}")
    tbl.add_row("Elapsed",                  f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return {r["package"] for r in out}


def load_cpp_packages() -> set[str]:
    if not os.path.exists(RAW_PACKAGES):
        return set()
    with open(RAW_PACKAGES, newline="", encoding="utf-8") as f:
        return {row["package"] for row in csv.DictReader(f)}


def load_aliases() -> dict[str, str]:
    """Return {old_name: current_name} from aliases.csv."""
    if not os.path.exists(RAW_ALIASES):
        return {}
    with open(RAW_ALIASES, newline="", encoding="utf-8") as f:
        return {row["old"]: row["current"] for row in csv.DictReader(f)}


# ── step 2: popcon via Wayback ────────────────────────────────────────────────

def pick_snapshots_by_year(years: list[int]) -> dict[int, str]:
    """Return {year: wayback_timestamp} — pick the snapshot closest to Dec 31 per year."""
    params = {
        "url": POPCON_URL, "output": "json",
        "from": f"{years[0]}0101", "to": f"{years[-1]}1231",
        "filter": ["statuscode:200", "mimetype:application/x-gzip"],
        "collapse": "timestamp:8",  # one per day
    }
    r = requests.get(WAYBACK_CDX, params=params, timeout=TIMEOUT,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    if not data:
        return {}
    # first row is header: ["urlkey","timestamp","original",...]
    ts_idx = data[0].index("timestamp")
    all_ts = [row[ts_idx] for row in data[1:]]

    picked: dict[int, str] = {}
    for yr in years:
        target = f"{yr}1231"
        candidates = [ts for ts in all_ts if ts[:4] == str(yr)]
        if not candidates:
            continue
        picked[yr] = min(candidates, key=lambda ts: abs(int(ts[:8]) - int(target)))
    return picked


def parse_popcon(raw: bytes) -> dict[str, int]:
    """Parse popcon by_inst.gz body → {package: installs}."""
    text = gzip.decompress(raw).decode("utf-8", errors="replace")
    result: dict[str, int] = {}
    for line in text.splitlines():
        # Lines look like:   1 libc6 211234 50123 160011 1100 0 (maintainer)
        # Skip comments and totals
        if not line or line.startswith("#") or line.startswith("--") or "Total" in line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        # parts[0]=rank, parts[1]=package, parts[2]=installs
        pkg = parts[1]
        try:
            installs = int(parts[2])
        except ValueError:
            continue
        result[pkg] = installs
    return result


def step_popcon(years: list[int], cpp_pkgs: set[str], alias_names: set[str], limit: int | None) -> None:
    console.rule("[bold cyan]Step 2 — popcon yearly installs (Wayback Machine)")
    console.print(f"  Years: [cyan]{years}[/cyan]")
    console.print("  Finding snapshots …")
    snapshots = pick_snapshots_by_year(years)

    tbl = Table(show_header=True, box=None, header_style="dim", padding=(0, 2))
    tbl.add_column("Year"); tbl.add_column("Wayback timestamp")
    for y in years:
        ts = snapshots.get(y, "[red]— no snapshot[/red]")
        tbl.add_row(str(y), ts)
    console.print(tbl)

    rows: list[dict] = []
    for yr, ts in snapshots.items():
        url = WAYBACK_REPLAY.format(ts=ts)
        t0 = time.perf_counter()
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = parse_popcon(r.content)
        elapsed = time.perf_counter() - t0

        # Filter to C/C++ packages — include alias old-names too, so pre-t64
        # historical popcon rows aren't lost (they get collapsed in process_data).
        if cpp_pkgs:
            keep = cpp_pkgs | alias_names
            filtered = {p: n for p, n in data.items() if p in keep}
        else:
            filtered = data

        if limit:
            filtered = dict(sorted(filtered.items(), key=lambda kv: kv[1], reverse=True)[:limit])

        for pkg, installs in filtered.items():
            rows.append({"package": pkg, "year": yr, "downloads": installs})

        console.print(
            f"  [green]{yr}[/green]  {len(filtered):,} packages  "
            f"(all={len(data):,}, max_installs={max(data.values()) if data else 0:,}, {elapsed:.1f}s)"
        )

    rows.sort(key=lambda r: (r["package"], r["year"]))
    atomic_write(RAW_DOWNLOADS, rows, ["package", "year", "downloads"])
    console.print(f"  → wrote {len(rows):,} rows to {RAW_DOWNLOADS}")


# ── step 3: Packages.xz → deps + metadata ─────────────────────────────────────

_VER_RE = re.compile(r"\(([^)]+)\)")


def parse_depends(field: str) -> list[tuple[str, str]]:
    """Parse a Depends:-style field into (name, version_constraint) pairs.

    Handles alternatives (foo | bar — keeps only the first), version constraints,
    and arch qualifiers (foo:any).
    """
    out: list[tuple[str, str]] = []
    for group in field.split(","):
        group = group.strip()
        if not group:
            continue
        # Keep the first alternative only (foo | bar → foo)
        first = group.split("|")[0].strip()
        m = _VER_RE.search(first)
        version = m.group(1).strip() if m else ""
        name = _VER_RE.sub("", first).strip()
        # Strip :arch qualifier
        name = name.split(":")[0].strip()
        if name:
            out.append((name, version))
    return out


def parse_relation_names(field: str) -> list[str]:
    """Extract bare package names from a Provides/Replaces/Depends-style field."""
    names: list[str] = []
    for group in field.split(","):
        first = group.split("|")[0].strip()
        name = _VER_RE.sub("", first).strip().split(":")[0].strip()
        if name:
            names.append(name)
    return names


def extract_t64_alias(stanza: dict) -> str | None:
    """For a t64 package, return the pre-transition name it replaces (or None).

    Uses `Replaces:` first (strongest signal — explicit successor claim),
    falls back to `Provides:` (virtual package name match).
    """
    pkg = stanza.get("Package", "")
    if not pkg.endswith("t64"):
        return None
    old_name = pkg[:-3]  # strip "t64"
    for field in ("Replaces", "Provides"):
        names = parse_relation_names(stanza.get(field, ""))
        if old_name in names:
            return old_name
    return None


def parse_packages_file(stream: io.TextIOBase) -> list[dict]:
    """Yield one dict per Package stanza. Handles RFC822 folding."""
    stanzas: list[dict] = []
    current: dict = {}
    key: str | None = None
    for raw in stream:
        line = raw.rstrip("\n")
        if not line:
            if current:
                stanzas.append(current)
                current = {}
            key = None
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if key:
                current[key] = current.get(key, "") + " " + line.strip()
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            key = k.strip()
            current[key] = v.strip()
    if current:
        stanzas.append(current)
    return stanzas


def step_index(cpp_pkgs: set[str], limit: int | None) -> None:
    console.rule("[bold cyan]Step 3 — Packages.xz → deps + metadata")
    console.print(f"  Fetching [cyan]{PACKAGES_URL}[/cyan] …")
    t0 = time.perf_counter()
    r = requests.get(PACKAGES_URL, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    console.print(f"  Downloaded {len(r.content) / 1e6:.1f} MB in {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    raw = lzma.decompress(r.content).decode("utf-8", errors="replace")
    stanzas = parse_packages_file(io.StringIO(raw))
    console.print(f"  Parsed {len(stanzas):,} binary package stanzas in {time.perf_counter() - t0:.1f}s")

    # One stanza per binary package per arch — dedupe by Package name (keep first)
    seen: set[str] = set()
    binary_stanzas: list[dict] = []
    for s in stanzas:
        pkg = s.get("Package")
        if not pkg or pkg in seen:
            continue
        seen.add(pkg)
        binary_stanzas.append(s)

    # Build t64 alias map from ALL stanzas (before C/C++ filter) so dep names
    # resolve correctly even when the old name is an indirect dep.
    alias_rows = [
        {"current": s["Package"], "old": old}
        for s in binary_stanzas
        if (old := extract_t64_alias(s)) is not None
    ]
    alias_rows.sort(key=lambda r: r["current"])
    atomic_write(RAW_ALIASES, alias_rows, ["current", "old"])
    console.print(f"  Found [cyan]{len(alias_rows):,}[/cyan] t64 aliases → {RAW_ALIASES}")

    if cpp_pkgs:
        binary_stanzas = [s for s in binary_stanzas if s["Package"] in cpp_pkgs]

    if limit:
        binary_stanzas = binary_stanzas[:limit]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    dep_rows: list[dict] = []
    meta_rows: list[dict] = []
    n_with_deps = 0
    n_with_github = 0
    github_re = re.compile(r"github\.com/([^/\s]+)/([^/\s#?]+)")

    for s in tqdm(binary_stanzas, desc="  parsing", unit="pkg"):
        pkg = s["Package"]

        # Debian convention: if Source is omitted, source name = binary name.
        # Source field can be "foo (1.2.3-1)" — strip the version.
        src_raw = s.get("Source", "").strip()
        source = src_raw.split()[0] if src_raw else pkg

        depends = s.get("Depends", "")
        pre = s.get("Pre-Depends", "")
        combined = ", ".join(x for x in (pre, depends) if x)
        deps = parse_depends(combined) if combined else []

        if deps:
            n_with_deps += 1
            for name, ver in deps:
                dep_rows.append({
                    "package": pkg, "dep_name": name,
                    "dep_version": ver, "fetched_at": now,
                })
        else:
            dep_rows.append({
                "package": pkg, "dep_name": "__none__",
                "dep_version": "", "fetched_at": now,
            })

        homepage = s.get("Homepage", "").strip()
        vcs = s.get("Vcs-Browser", "") or s.get("Vcs-Git", "")
        vcs = vcs.strip()
        section = s.get("Section", "").strip()

        if github_re.search(homepage) or github_re.search(vcs):
            n_with_github += 1

        meta_rows.append({
            "package": pkg, "source": source, "homepage": homepage,
            "vcs_browser": vcs, "section": section,
        })

    dep_rows.sort(key=lambda r: (r["package"], r["dep_name"]))
    meta_rows.sort(key=lambda r: r["package"])
    atomic_write(RAW_DEPS, dep_rows, ["package", "dep_name", "dep_version", "fetched_at"])
    atomic_write(RAW_METADATA, meta_rows, ["package", "source", "homepage", "vcs_browser", "section"])

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Binary stanzas parsed", f"{len(binary_stanzas):,}")
    tbl.add_row("With runtime deps",      f"{n_with_deps:,}")
    tbl.add_row("With github.com URL",    f"{n_with_github:,}")
    tbl.add_row("Dep edges written",      f"{sum(1 for r in dep_rows if r['dep_name'] != '__none__'):,}")
    console.print(tbl)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--step", choices=["packages", "popcon", "index", "all"], default="all")
    p.add_argument("--years", type=int, nargs="+", default=YEARS)
    p.add_argument("--limit", type=int, default=None,
                   help="Cap per-year popcon rows (keeps top-installs) / cap index packages")
    p.add_argument("--refresh", action="store_true",
                   help=f"Force refetch, ignoring the {TTL_DAYS}-day output TTL")
    p.add_argument("--offline", action="store_true",
                   help="Skip all network fetches (keep whatever is cached)")
    args = p.parse_args()

    console.rule("[bold]debian — fetch_debian_data")
    console.print(f"  Started : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  Step    : [cyan]{args.step}[/cyan]")
    if args.limit:
        console.print(f"  Limit   : [yellow]{args.limit}[/yellow] (test mode)")
    console.print()

    t_total = time.perf_counter()

    def _skip(label: str, *outputs: str) -> bool:
        """True when this step's outputs are all within TTL (or --offline)."""
        if args.offline:
            console.print(f"  [dim]{label}: skipped (--offline)[/dim]")
            return True
        if not args.refresh and all(file_is_fresh(o, TTL_DAYS) for o in outputs):
            console.print(f"  [dim]{label}: outputs fresh (< {TTL_DAYS}d) — skipping; --refresh to force[/dim]")
            return True
        return False

    if args.step in ("packages", "all") and not _skip("packages", RAW_PACKAGES, RAW_METADATA):
        step_packages()

    cpp_pkgs = load_cpp_packages()

    # Run index BEFORE popcon so aliases.csv exists and popcon can widen its
    # filter to include pre-t64 names that debtags doesn't tag.
    if args.step in ("index", "all") and not _skip("index", RAW_DEPS, RAW_ALIASES):
        if not cpp_pkgs:
            console.print("[yellow]WARNING:[/yellow] no package list — index step will include all packages")
        step_index(cpp_pkgs, args.limit)

    if args.step in ("popcon", "all") and not _skip("popcon", RAW_DOWNLOADS):
        if not cpp_pkgs:
            console.print("[yellow]WARNING:[/yellow] no package list — popcon will include all packages")
        alias_map = load_aliases()
        alias_names = set(alias_map.keys()) | set(alias_map.values())
        step_popcon(args.years, cpp_pkgs, alias_names, args.limit)

    console.print(f"\n[bold green]Done in {time.perf_counter() - t_total:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
