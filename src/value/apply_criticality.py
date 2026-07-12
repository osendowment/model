#!/usr/bin/env python3
"""Apply the value factors onto value.csv as `openssf_crit`, `eco_crit`, `value_score`.

Roll-up step, run after `unify_value_data` / `build_validation` (both rebuild
value.csv without these columns' values). Joins two per-repo signals onto
value.csv and blends them into a single 0–100 `score`:

  - `openssf_crit` — the OpenSSF criticality score, fetched 0–1 by
    `src.sources.openssf.criticality` and stored ·100 (0–100, two decimals,
    xx.xx). GitHub-only (the tool doesn't support other hosts). Joined by repo
    id (value.csv `gh/<id>` ↔ criticality.csv id) with a canonical-slug
    fallback.
  - `eco_crit` — the ecosyste.ms critical flag, fetched by
    `src.sources.ecosystems.criticality`: `100` = on the critical list, `0` =
    explicitly not on it (`critical=False`), blank = unknown — the fetch didn't
    resolve, the registry omitted the flag (common for spack/debian cpp
    packages), or the repo wasn't checked. Covers GitHub AND GitLab, so a GitLab
    class-A repo with an explicit flag gets an importance signal even though it
    has no `openssf_crit`. Joined by repo id, then by normalized git URL.
  - `value_score` — a 0–100 pro-rata blend of whichever of the three components
    (`openssf_crit`, `eco_crit`, `top_eco_pct`) are present, renormalized
    by their weight total (settings.json → value_score). A row needs at least
    `min_components` present or `value_score` stays blank. See docs/value.md.

An error/unresolved fetch must never masquerade as a real 0: openssf error rows
(`status != ok`) and eco_crit `ok=False` rows leave their column blank, and a
blank component is simply dropped from the blend (not treated as zero).

Coverage contract: **every valid class-A GitHub repo must end up with a
non-empty `openssf_crit`** — that is the fetcher's scope (archived included),
so a blank there means a missing/failed fetch, never "not applicable".
This script reports violations; `scripts/pipeline_health.py` enforces the
same rule as a pipeline gate.

Idempotent: re-running re-joins from the same inputs.

Usage:
    uv run python -m src.value.apply_criticality
"""

from __future__ import annotations

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.params import (
    VALUE_SCORE_CENTRALITY_WEIGHT,
    VALUE_SCORE_PR_WEIGHT,
    VALUE_SCORE_CRIT_WEIGHT,
    VALUE_SCORE_ECO_CRIT_WEIGHT,
    VALUE_SCORE_MIN_COMPONENTS,
)
from src.value.unify_value_data import OUTPUT_FILE, write_value_data

console = Console()

ROOT = Path(__file__).resolve().parents[2]
CRITICALITY_FILE = ROOT / "data" / "sources" / "openssf" / "criticality.csv"
ECO_CRIT_FILE = ROOT / "data" / "sources" / "ecosystems" / "criticality.csv"


def load_criticality(path: Path = CRITICALITY_FILE) -> tuple[dict, dict]:
    """({repo_id: score}, {slug: score}) from status=ok criticality rows.

    Ids are the canonical `gh/<id>` form on both sides of the join
    (criticality.csv and value.csv), so no prefix juggling here."""
    by_id: dict[str, str] = {}
    by_slug: dict[str, str] = {}
    if not path.exists():
        return by_id, by_slug
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip().lower() != "ok":
                continue
            score = (row.get("criticality_score") or "").strip()
            if not score:
                continue
            rid = (row.get("repo_id") or "").strip()
            slug = (row.get("repo") or "").strip().lower()
            if rid:
                by_id[rid] = score
            if slug:
                by_slug[slug] = score
    return by_id, by_slug


def _norm_url(git_url: str) -> str:
    """Lowercased repository URL without a trailing `.git` / slash — the join key
    shared with ecosyste.ms criticality.csv's `repository_url`."""
    u = (git_url or "").strip()
    if u.endswith(".git"):
        u = u[:-4]
    return u.rstrip("/").lower()


def _eco_crit_value(row: dict) -> str:
    """value.csv `eco_crit` for one ecosyste.ms criticality row: "100" / "0" / "".

    Only an EXPLICIT flag becomes 0/100: `100` = on the critical list, `0` =
    explicitly not on it (`critical=False`). Blank whenever the signal is
    unknown — the fetch didn't resolve (`ok != True`), or it resolved but the
    registry omitted the `critical` field (common for spack/debian cpp packages).
    A blank is never a real 0: "checked, no flag" is unknown, not "not critical"."""
    if (row.get("ok") or "").strip().lower() != "true":
        return ""
    crit = (row.get("critical") or "").strip()
    if crit == "True":
        return "100"
    if crit == "False":
        return "0"
    return ""  # resolved but the registry gave no critical flag → unknown


def load_eco_crit(path: Path = ECO_CRIT_FILE) -> tuple[dict, dict]:
    """({repo_id: eco_crit}, {normalized_url: eco_crit}) from the ecosyste.ms
    criticality table. Both maps let apply() join by value.csv's unified repo_id
    first, then fall back to the normalized git URL."""
    by_id: dict[str, str] = {}
    by_url: dict[str, str] = {}
    if not path.exists():
        return by_id, by_url
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ec = _eco_crit_value(row)
            rid = (row.get("repo_id") or "").strip()
            url = _norm_url(row.get("repository_url") or "")
            if rid:
                by_id[rid] = ec
            if url:
                by_url[url] = ec
    return by_id, by_url


def compute_score(openssf_crit: float | None, eco_crit: float | None,
                  top_eco_pct: float | None,
                  pr_score: float | None = None) -> float | None:
    """value.csv `value_score` — a 0–100 pro-rata blend of the present components.

    Each argument is the raw component value, or ``None`` when absent for this
    repo:
      - ``openssf_crit`` — OpenSSF criticality, already ·100 (0–100, as stored).
      - ``eco_crit``     — ecosyste.ms critical flag, already 0/100.
      - ``top_eco_pct``  — PageRank percentile in the top ecosystem, already 0–100.
      - ``pr_score``     — cross-ecosystem dependency-mass score, already 0–100.

    Present components are weighted (settings.json → value_score) and the sum is
    renormalized by their weight total, so a repo missing some still lands on the
    same 0–100 scale. Returns ``None`` — a blank `score` — when fewer than
    ``VALUE_SCORE_MIN_COMPONENTS`` components are present, so a single lone
    signal never masquerades as a full score."""
    parts: list[tuple[float, float]] = []  # (weight, value on 0–100)
    if openssf_crit is not None:
        parts.append((VALUE_SCORE_CRIT_WEIGHT, openssf_crit))
    if eco_crit is not None:
        parts.append((VALUE_SCORE_ECO_CRIT_WEIGHT, eco_crit))
    if top_eco_pct is not None:
        parts.append((VALUE_SCORE_CENTRALITY_WEIGHT, top_eco_pct))
    if pr_score is not None:
        parts.append((VALUE_SCORE_PR_WEIGHT, pr_score))
    if len(parts) < VALUE_SCORE_MIN_COMPONENTS:
        return None
    wsum = sum(w for w, _ in parts)
    if wsum <= 0:
        return None
    return sum(w * v for w, v in parts) / wsum


def _as_float(value: str | None) -> float | None:
    """Parse a non-blank numeric cell to float; blank/garbage → None (absent)."""
    s = (value or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def apply(value_file: Path = OUTPUT_FILE,
          criticality_file: Path = CRITICALITY_FILE,
          eco_crit_file: Path = ECO_CRIT_FILE) -> list[dict]:
    """Join `openssf_crit` + `eco_crit`, blend `score`, onto value.csv in place."""
    by_id, by_slug = load_criticality(criticality_file)
    eco_by_id, eco_by_url = load_eco_crit(eco_crit_file)
    with open(value_file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        row["openssf_crit"] = ""
        row["eco_crit"] = ""
        row["value_score"] = ""
        platform = (row.get("platform") or "").strip().lower()
        rid = (row.get("repo_id") or "").strip()
        slug = (row.get("repo") or "").strip().lower()
        url = _norm_url(row.get("git_url") or "")

        # openssf_crit — GitHub-only tool; join by id, fall back to slug.
        # criticality.csv carries the raw 0–1 score; value.csv stores it ·100
        # (0–100, two decimals) so the blend below reads exactly what's on disk.
        openssf: float | None = None
        if platform == "github":
            crit = by_id.get(rid) if rid else None
            if crit is None:
                crit = by_slug.get(slug)
            raw = _as_float(crit)
            if raw is not None:
                openssf = round(raw * 100.0, 2)
                row["openssf_crit"] = f"{openssf:.2f}"

        # eco_crit — GitHub + GitLab; join by id, fall back to URL. An ok=False
        # row matches as "" (unresolved) — kept distinct from None (no row at
        # all), though both leave the column blank / the component absent.
        ec_str: str | None = None
        if rid and rid in eco_by_id:
            ec_str = eco_by_id[rid]
        elif url and url in eco_by_url:
            ec_str = eco_by_url[url]
        row["eco_crit"] = ec_str or ""
        eco: float | None = float(ec_str) if ec_str in ("0", "100") else None

        # top_eco_pct — already a 0–100 percentile.
        top_eco_pct = _as_float(row.get("top_eco_pct"))

        # pr_score — cross-ecosystem dependency mass, already 0–100.
        pr_score = _as_float(row.get("pr_score"))

        blended = compute_score(openssf, eco, top_eco_pct, pr_score)
        if blended is not None:
            row["value_score"] = f"{blended:.2f}"

    # Ranked by value score, highest first. Unscored rows (below the
    # 2-component floor — mostly class B/C) sink to the end, kept in their
    # top_eco_pct-desc importance order, then repo for determinism.
    def _sort_key(r: dict) -> tuple:
        sc = (r.get("value_score") or "").strip()
        tp = (r.get("top_eco_pct") or "").strip()
        return (sc == "", -float(sc) if sc else 0.0,
                -float(tp) if tp else 0.0, (r.get("repo") or "").lower())

    rows.sort(key=_sort_key)
    write_value_data(rows, value_file)
    return rows


def report(rows: list[dict]) -> list[str]:
    """Print coverage; return the violating repos (valid A github, blank crit)."""
    gate = [r for r in rows
            if (r.get("platform") or "").lower() == "github"
            and (r.get("git_valid") or "") == "True"
            and (r.get("class") or "") == "A"]
    violations = [r["repo"] for r in gate if not (r.get("openssf_crit") or "").strip()]

    filled_oc = sum(1 for r in rows if (r.get("openssf_crit") or "").strip())
    filled_ec = sum(1 for r in rows if (r.get("eco_crit") or "").strip())
    scored = [r for r in rows if (r.get("value_score") or "").strip()]
    scores = [float(r["value_score"]) for r in scored]
    gl_total = sum(1 for r in rows if (r.get("platform") or "").lower() == "gitlab")
    gl_scored = sum(1 for r in scored if (r.get("platform") or "").lower() == "gitlab")

    table = Table(title="[bold]value.csv openssf_crit / eco_crit / value_score coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Scope", style="bold")
    table.add_column("Filled", justify="right")
    table.add_column("Total", justify="right")
    table.add_row("openssf_crit (all rows)", f"{filled_oc:,}", f"{len(rows):,}")
    table.add_row("eco_crit (all rows)", f"{filled_ec:,}", f"{len(rows):,}")
    table.add_row("valid class-A github (openssf gate)",
                  f"{len(gate) - len(violations):,}", f"{len(gate):,}",
                  style="bold")
    table.add_row("rows with value_score", f"{len(scored):,}", f"{len(rows):,}")
    table.add_row("gitlab rows scored", f"{gl_scored:,}", f"{gl_total:,}")
    console.print(table)
    if scores:
        console.print(f"[dim]value_score  min/mean/max: {min(scores):.1f} / "
                      f"{sum(scores) / len(scores):.1f} / {max(scores):.1f}[/dim]")

    if violations:
        console.print(f"[red]GATE VIOLATED — {len(violations)} valid class-A "
                      f"github repo(s) without an openssf_crit score:[/red]")
        for repo in violations[:20]:
            console.print(f"  [red]{repo}[/red]")
        if len(violations) > 20:
            console.print(f"  [red]… and {len(violations) - 20} more[/red]")
    else:
        console.print("[green]Gate satisfied: every valid class-A github repo "
                      "has an openssf_crit score.[/green]")
    return violations


def main() -> None:
    console.print("[bold]Applying openssf_crit + eco_crit + value_score onto value.csv...[/bold]\n")
    rows = apply()
    report(rows)


if __name__ == "__main__":
    main()
