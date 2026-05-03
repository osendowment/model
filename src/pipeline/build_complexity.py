#!/usr/bin/env python3
"""Build data/complexity.csv — codebase complexity per eligible repo.

Reads:
    data/eligibility-data.csv               — eligible repo set
    data/github/git/commits-years.csv       — per (repo, year) last_sha + commits
    data/github/git/loc.csv                 — wide per-year (2021..2025) loc=total lines
    data/github/git/sloc.csv                — wide per-year, code-only
    data/github/git/scc-complexity.csv      — wide per-year
    data/github/git/scc-density.csv         — wide per-year
    data/github/git/complexity.csv          — single-shot fallback (current HEAD)

Writes:
    data/complexity.csv  with columns:
        repo, repo_id,
        loc_2025_eoy, sloc_2025_eoy,
        scc_complexity_2025_eoy, scc_density_2025_eoy,
        loc_year   (year of snapshot used: 2021..2025 or "current")

Period: [2025 EOY] = scc analysis of the last commit on the default
branch ≤ 2025-12-31.

Selection logic:
1. Read commits-years.csv → target_year = most recent y ≤ 2025 with
   commits > 0. (If a repo has no commits in 2021–2025 it falls
   straight to step 3.)
2. If wide[target_year] is populated and non-zero, use it (this is the
   true scc snapshot at that year's last SHA).
3. Else use single-shot complexity.csv (current HEAD ≈ EOY 2025 for
   repos with little 2026 activity, AND ≈ wide[target_year] for repos
   whose default branch hasn't moved since target_year).
4. Else walk wide 2025→2021 for any populated year.
5. Else empty.

Naming note: in the wide `loc.csv` / `sloc.csv` pair, `loc` is *total*
lines (incl. comments/blanks) and `sloc` is scc "Code" (source lines
only). The single-shot `complexity.csv` confusingly uses `loc` to mean
scc Code; we therefore map it into both `loc_2025_eoy` and
`sloc_2025_eoy` (best available) when falling back.

Usage:
    uv run python -m src.pipeline.build_complexity
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.repos import load_eligible_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
GIT_DIR = DATA_DIR / "github" / "git"
OUTPUT_FILE = DATA_DIR / "complexity.csv"

# Wide CSVs (one column per year, header `repo,2021,2022,2023,2024,2025`).
SOURCES = {
    "loc": GIT_DIR / "loc.csv",
    "sloc": GIT_DIR / "sloc.csv",
    "scc_complexity": GIT_DIR / "scc-complexity.csv",
    "scc_density": GIT_DIR / "scc-density.csv",
}
# Single-shot fallback (one row per repo, columns: repo,files,loc,uloc,
# complexity,complexity_density,...). `loc` here is scc Code (sloc-style).
SINGLE_SHOT_FILE = GIT_DIR / "complexity.csv"
COMMITS_YEARS_FILE = GIT_DIR / "commits-years.csv"
YEARS_DESC = ["2025", "2024", "2023", "2022", "2021"]

FIELDS = [
    "repo", "repo_id",
    "loc_2025_eoy", "sloc_2025_eoy",
    "scc_complexity_2025_eoy", "scc_density_2025_eoy",
    "loc_year",
]


def _load_wide(path: Path) -> dict[str, dict[str, str]]:
    """Load {repo: {year: value}} from a wide per-year CSV. Empty if missing."""
    out: dict[str, dict[str, str]] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            repo = (row.get("repo") or "").strip().lower()
            if not repo:
                continue
            out[repo] = {y: (row.get(y) or "").strip() for y in YEARS_DESC}
    return out


def _pick_year(per_year: dict[str, str]) -> tuple[str, str]:
    """Return (year, value) for the most recent populated, non-zero year.

    Walk 2025→2021. A blank or "0" value is treated as "no snapshot for
    that year" (the upstream build_git pipeline writes 0 when the repo
    had no commits in that year). Falling back lets repos with last
    activity in 2023 still surface a real LOC number.
    """
    for y in YEARS_DESC:
        v = per_year.get(y, "").strip()
        if not v:
            continue
        # 0 values are treated as "no snapshot in that year"
        try:
            if float(v) == 0:
                continue
        except ValueError:
            continue
        return y, v
    return "", ""


def _load_single_shot() -> dict[str, dict[str, str]]:
    """Load {repo: row} from data/github/git/complexity.csv (one snapshot per repo)."""
    out: dict[str, dict[str, str]] = {}
    if not SINGLE_SHOT_FILE.exists():
        return out
    with open(SINGLE_SHOT_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = row
    return out


def _load_target_year() -> dict[str, str]:
    """Return {repo: target_year_str} — most recent year ≤ 2025 with commits>0.

    Empty for repos with no entry in commits-years.csv or no commits at all
    in 2021–2025 (truly dormant since before 2021).
    """
    by_repo: dict[str, dict[int, int]] = {}
    if not COMMITS_YEARS_FILE.exists():
        return {}
    with open(COMMITS_YEARS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            try:
                year = int((row.get("year") or "").strip())
                commits = int((row.get("commits") or "0").strip())
            except ValueError:
                continue
            if 2021 <= year <= 2025:
                by_repo.setdefault(slug, {})[year] = commits

    out: dict[str, str] = {}
    for slug, years in by_repo.items():
        for y in (2025, 2024, 2023, 2022, 2021):
            if years.get(y, 0) > 0:
                out[slug] = str(y)
                break
    return out


def _from_single_shot(ss: dict | None) -> tuple[str, str, str, str, str] | None:
    """Return (loc, sloc, complexity, density, year_label) or None."""
    if not ss or not ss.get("loc"):
        return None
    # In single-shot, `loc` = scc Code (sloc-style) — we map it into BOTH
    # loc and sloc because the wide-style "total lines incl. comments" isn't
    # stored in the single-shot file.
    code = ss.get("loc", "")
    cplx = ss.get("complexity", "")
    dens = ss.get("complexity_density", "")
    return code, code, cplx, dens, "current"


def _from_wide(wides: dict, repo: str, year: str) -> tuple[str, str, str, str] | None:
    """(loc, sloc, complexity, density) from wide files at year, or None if zero/empty."""
    val = (wides["loc"].get(repo, {}).get(year) or "").strip()
    try:
        if not val or float(val) == 0:
            return None
    except ValueError:
        return None
    return (
        val,
        wides["sloc"].get(repo, {}).get(year, ""),
        wides["scc_complexity"].get(repo, {}).get(year, ""),
        wides["scc_density"].get(repo, {}).get(year, ""),
    )


def build() -> list[dict]:
    eligible = load_eligible_repos()

    wides = {name: _load_wide(p) for name, p in SOURCES.items()}
    single = _load_single_shot()
    target_year = _load_target_year()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        ty = target_year.get(repo)  # str like "2025" or None for dormant repos

        # Strategy:
        #   1. wide[target_year] — true scc snapshot at year's last_sha
        #   2. single-shot complexity.csv (current HEAD ≈ target_year EOY for
        #      repos whose default branch hasn't moved since target_year)
        #   3. walk wide 2025→2021 for any populated year (last resort)
        loc_val = sloc_val = cplx_val = dens_val = loc_year = ""

        if ty:
            wt = _from_wide(wides, repo, ty)
            if wt:
                loc_val, sloc_val, cplx_val, dens_val = wt
                loc_year = ty

        if not loc_year:
            ss_tuple = _from_single_shot(single.get(repo))
            if ss_tuple:
                loc_val, sloc_val, cplx_val, dens_val, loc_year = ss_tuple

        if not loc_year:
            # last resort: any populated wide year (2025→2021)
            for y in YEARS_DESC:
                wt = _from_wide(wides, repo, y)
                if wt:
                    loc_val, sloc_val, cplx_val, dens_val = wt
                    loc_year = y
                    break

        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "loc_2025_eoy": loc_val,
            "sloc_2025_eoy": sloc_val,
            "scc_complexity_2025_eoy": cplx_val,
            "scc_density_2025_eoy": dens_val,
            "loc_year": loc_year,
        })
    return rows


def main() -> None:
    console.print("[bold]Building complexity.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Complexity coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in ("loc_2025_eoy", "sloc_2025_eoy",
                "scc_complexity_2025_eoy", "scc_density_2025_eoy"):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    # Year-fallback breakdown
    year_dist = Table(title="\n[bold]LOC snapshot year used[/bold]",
                      show_header=True, header_style="bold dim", padding=(0, 1))
    year_dist.add_column("Year", style="bold")
    year_dist.add_column("Repos", justify="right")
    year_dist.add_column("%", justify="right")
    from collections import Counter
    yc = Counter(r["loc_year"] or "(none)" for r in rows)
    for y in ("2025", "2024", "2023", "2022", "2021", "current", "(none)"):
        n = yc.get(y, 0)
        pct = 100 * n / total if total else 0
        year_dist.add_row(y, f"{n:,}", f"{pct:.1f}%")
    console.print(year_dist)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
