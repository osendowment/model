#!/usr/bin/env python3
"""Generate every pipeline statistic that lives in `docs/stats.md`.

`docs/stats.md` is the single source of truth for the model's counts, funnels,
coverage and distribution figures. This script recomputes ALL of them from the
live CSVs so a refresh is one command instead of a hand-audit:

  uv run python scripts/stats.py              # rich dashboard (read the numbers)
  uv run python scripts/stats.py --markdown   # emit the stats.md tables verbatim
  uv run python scripts/stats.py --check       # diff computed headline numbers
                                               # against docs/stats.md (drift gate)

Every figure is derived from data, never hard-coded:
  - Value tables   ← data/value/value.csv (+ data/value/stats.csv top-of-funnel,
                     per-eco data/sources/<eco>/results.csv).
  - Risk tables    ← data/risk/*.csv scoped to `load_top_repos()` (the top repos),
                     plus the source CSVs each funnel step reads.

The Risk denominator is the top-repo set (`load_top_repos()`), matching what the
builders actually score; a blank score is therefore a real coverage gap, except
where the metric legitimately doesn't exist (documented in stats.md prose).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.common.repos import load_top_repos  # noqa: E402

console = Console()

ECOSYSTEMS = ["npm", "pypi", "crates", "cpp"]
CLASS_COL = {"npm": "class_npm", "pypi": "class_pypi",
             "crates": "class_crates", "cpp": "class_cpp"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _load(path: str) -> list[dict]:
    with (ROOT / path).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _present(v: str | None) -> bool:
    return (v or "").strip() != ""


def _num(v: str | None) -> float | None:
    v = (v or "").strip()
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("true", "1")


def _quantiles(values: list[float]) -> tuple[float, ...] | None:
    if not values:
        return None
    a = np.array(values)
    return tuple(float(np.percentile(a, q)) for q in (0, 25, 50, 75, 100))


def _fmt(val: float | None, kind: str) -> str:
    """Render a raw metric value in its natural form.

    `int` = comma-grouped integer (LOC, bus factor, HHI, CVE count);
    `f1`/`f2` = fixed decimals (OpenSSF 0–10 score, per-contributor ratios).
    """
    if val is None:
        return ""
    if kind == "int":
        return f"{round(val):,}"
    if kind == "f1":
        return f"{val:.1f}"
    if kind == "f2":
        return f"{val:.2f}"
    return str(val)


# ── scope ────────────────────────────────────────────────────────────────────

def scope_set() -> set[str]:
    """The top repos the risk pipeline scores — the Risk-table denominator."""
    return {e.repo for e in load_top_repos() if e.repo}


# ── value ────────────────────────────────────────────────────────────────────

def _archived_map() -> dict[str, bool]:
    """Slug (and full_name) → archived flag, from github/repos.csv."""
    out: dict[str, bool] = {}
    for r in _load("data/sources/github/repos.csv"):
        a = _truthy(r.get("archived"))
        for k in ("repo", "full_name"):
            s = (r.get(k) or "").strip().lower()
            if s:
                out[s] = a
    return out


def value_stats() -> dict:
    rows = _load("data/value/value.csv")
    arch = _archived_map()
    m = len(rows)
    gh = sum(1 for r in rows if _present(r.get("github_repo")))
    git = sum(1 for r in rows if _present(r.get("git_url")))
    valid = sum(1 for r in rows if _truthy(r.get("valid")))
    orphan = m - gh

    def _is_active(r: dict) -> bool:
        s = (r.get("github_repo") or "").strip().lower()
        return bool(s) and not arch.get(s, False)

    # class distribution: per-ecosystem column + strongest cross-eco `class`
    classes = {}
    for cls in ("A", "B", "C"):
        per_eco = {eco: sum(1 for r in rows if (r.get(CLASS_COL[eco]) or "").strip() == cls)
                   for eco in ECOSYSTEMS}
        per_eco["strongest"] = sum(1 for r in rows if (r.get("class") or "").strip() == cls)
        classes[cls] = per_eco

    # identity coverage per strongest class (distinct repos)
    by_class = {}
    for cls in ("A", "B", "C"):
        sub = [r for r in rows if (r.get("class") or "").strip() == cls]
        by_class[cls] = {
            "repos": len(sub),
            "github": sum(1 for r in sub if _present(r.get("github_repo"))),
            "active": sum(1 for r in sub if _is_active(r)),
            "git": sum(1 for r in sub if _present(r.get("git_url"))),
            "valid": sum(1 for r in sub if _truthy(r.get("valid"))),
        }

    # Packages per repo class, from value.csv's own `packages` column (every
    # package belongs to exactly one repo group, so this is A/B/C-only and sums
    # to the results universe). `ght` = the subset in GitHub-identified groups.
    pkg_class = {"A": 0, "B": 0, "C": 0}
    ght_class = {"A": 0, "B": 0, "C": 0}
    for r in rows:
        c = (r.get("class") or "").strip()
        if c not in pkg_class:
            continue
        p = int(r.get("packages") or 0)
        pkg_class[c] += p
        if _present(r.get("github_repo")):
            ght_class[c] += p

    # per-ecosystem funnel: top-of-funnel from stats.csv, tail from results.csv
    smatrix = {r["metric"]: r for r in _load("data/value/stats.csv")}
    funnel = {}
    for eco in ECOSYSTEMS:
        rr = _load(f"data/sources/{eco}/results.csv")
        n = len(rr)
        wgh = sum(1 for r in rr if _present(r.get("github_repo")))
        wgit = sum(1 for r in rr if _present(r.get("git")))
        funnel[eco] = {
            "top": int(smatrix.get("packages_top", {}).get(eco, 0) or 0),
            "deps": int(smatrix.get("packages_with_deps", {}).get(eco, 0) or 0),
            "results": n, "github": wgh, "git": wgit,
        }

    return {
        "rows": m, "github": gh, "git": git, "valid": valid, "orphan": orphan,
        "pkg_class": pkg_class, "ght_class": ght_class,
        "classes": classes, "by_class": by_class, "funnel": funnel,
    }


# ── risk ─────────────────────────────────────────────────────────────────────

# (component, dimension CSV, score col, [(subcomponent label, RAW column, fmt)]).
# The component row is the 0–100 risk score; the subcomponent rows show the
# NATURAL metric distribution (bus factor, LOC, $ …) — not the 0–100 percentile
# the score is built from, which is 0/25/50/75/100 by construction and tells you
# nothing. fmt: "int" comma-grouped, "f1"/"f2" fixed decimals.
RISK_COMPONENTS = [
    ("Concentration", "concentration", "score", [
        ("bus factor", "bf_commits_git_5y", "int"),
        ("HHI", "hhi_commits_git_5y", "int")]),
    ("Complexity", "complexity", "score", [
        ("lines of code", "loc_eoy", "int"),
        ("cyclomatic max", "cyclomatic_max", "int")]),
    ("Security", "security", "score", [
        ("OpenSSF score (0–10)", "openssf_score", "f1"),
        ("CVE count 5y", "cve_count_5y", "int")]),
    ("Workload", "workload", "score", [
        ("LOC / contributor", "loc_per_ac", "int"),
        ("CVE / contributor", "cve_per_ac", "f2"),
        ("net-new-issues / contributor", "nni_per_ac", "f2")]),
]

# (dimension CSV, [(label, kind, column/expr)]) for each per-dimension funnel.
# kind: "present" = non-blank; "gt0" = numeric > 0; "eq" = numeric == 0/1; "bool".
RISK_FUNNELS = {
    "concentration": [("bus factor / HHI (git 5y) computed", "present", "bf_commits_git_5y_p"),
                      ("bus factor / HHI (GitHub) computed", "present", "bf_commits_gh_alltime_p"),
                      ("Concentration score", "present", "score")],
    "complexity": [("lines of code (scc)", "present", "loc_eoy"),
                   ("cyclomatic max (lizard)", "present", "cyclomatic_max"),
                   ("cognitive max (lizard)", "present", "cognitive_max"),
                   ("churn 5y", "present", "churn_5y_total"),
                   ("Complexity score", "present", "score")],
    "security": [("OpenSSF score present", "present", "openssf_score"),
                 ("semgrep SAST present", "present", "sast_findings_total"),
                 ("CVE count 5y > 0", "gt0", "cve_count_5y"),
                 ("OSS-Fuzz enrolled", "bool", "ossfuzz_enrolled"),
                 ("CII Best Practices badge", "present", "bestpractices_badge_id"),
                 ("Security score", "present", "score")],
    "workload": [("issues data present", "present", "issues_opened_5y"),
                 ("per-AC ratios (loc/cve/nni) computed", "present", "loc_per_ac_p"),
                 ("`issue_close_ratio` computed", "present", "issue_close_ratio"),
                 ("`issue_trend_score` computed", "present", "issue_trend_score"),
                 ("Workload score", "present", "score")],
}


def _count(rows: list[dict], scope: set[str], kind: str, col: str) -> int:
    in_scope = (r for r in rows if r["repo"] in scope)
    if kind == "present":
        return sum(1 for r in in_scope if _present(r.get(col)))
    if kind == "gt0":
        return sum(1 for r in in_scope if (_num(r.get(col)) or 0) > 0)
    if kind == "bool":
        return sum(1 for r in in_scope if _truthy(r.get(col)))
    raise ValueError(kind)


def risk_stats() -> dict:
    scope = scope_set()
    n = len(scope)
    dims = {name: _load(f"data/risk/{name}.csv")
            for _, name, _, _ in RISK_COMPONENTS}
    risk = _load("data/risk/risk.csv")

    def col_dist(rows, col):
        vals = [v for v in (_num(r.get(col)) for r in rows if r["repo"] in scope)
                if v is not None]
        return len(vals), _quantiles(vals)

    # score distribution: component = the 0–100 score; subcomponents = raw metrics.
    # `column` is the source CSV column, kept separate from the human `label`.
    distribution = []
    for comp, name, score_col, subs in RISK_COMPONENTS:
        scored, q = col_dist(dims[name], score_col)
        distribution.append({"label": comp, "column": score_col,
                             "is_component": True, "fmt": "int",
                             "scored": scored, "q": q})
        for sub_label, raw_col, fmt in subs:
            scored, q = col_dist(dims[name], raw_col)
            distribution.append({"label": sub_label, "column": raw_col,
                                 "is_component": False, "fmt": fmt,
                                 "scored": scored, "q": q})
    o_scored, o_q = col_dist(risk, "score")
    distribution.append({"label": "Overall", "column": "score",
                         "is_component": True, "fmt": "int",
                         "scored": o_scored, "q": o_q})

    # per-dimension funnels
    funnels = {name: [(label, _count(dims[name], scope, kind, col))
                      for label, kind, col in steps]
               for name, steps in RISK_FUNNELS.items()}

    # intent / nonprofit coverage (from risk.csv — already loaded above)
    intent_count = _count(risk, scope, "bool", "intent")
    nonprofit_count = _count(risk, scope, "bool", "nonprofit")

    # headline prose figures
    conc = dims["concentration"]
    bf_vals = [_num(r.get("bf_commits_git_5y")) for r in conc if r["repo"] in scope]
    bf_computed = [v for v in bf_vals if v is not None]
    bf1 = sum(1 for v in bf_computed if v == 1)

    return {
        "scope": n, "distribution": distribution, "funnels": funnels,
        "intent_count": intent_count, "nonprofit_count": nonprofit_count,
        "overall_scored": o_scored, "overall_gap": n - o_scored,
        "bf1_pct": 100 * bf1 / len(bf_computed) if bf_computed else 0,
    }


# ── rendering: rich dashboard ────────────────────────────────────────────────

def _pct(n: int, d: int) -> str:
    if not d:
        return "—"
    p = 100 * n / d
    # stats.md convention: an exact 100% is written "100%", everything else 1dp.
    return "100%" if n == d else f"{p:.1f}%"


def dashboard(v: dict, r: dict) -> None:
    console.rule("[bold]Pipeline statistics[/bold]")
    console.print(f"value.csv rows: [bold]{v['rows']:,}[/bold]   "
                  f"risk scope (top repos): [bold]{r['scope']:,}[/bold]\n")

    t = Table(title="Value — identity coverage", header_style="bold dim")
    t.add_column("Step")
    for c in ("A", "B", "C", "Total"):
        t.add_column(c, justify="right")
    bc = v["by_class"]

    def _drow(label, d, bold=False):
        cells = [f"{x:,}" for x in (d["A"], d["B"], d["C"], d["A"] + d["B"] + d["C"])]
        t.add_row(label, *cells, style="bold" if bold else None)

    _drow("Packages", v["pkg_class"])
    _drow("GitHub total repos", v["ght_class"])
    _drow("GitHub unique repos", {c: bc[c]["github"] for c in "ABC"})
    _drow("Valid repos", {c: bc[c]["valid"] for c in "ABC"}, bold=True)
    console.print(t)

    t = Table(title="Value — class distribution", header_style="bold dim")
    t.add_column("Metric")
    for c in ("A", "B", "C"):
        t.add_column(c, justify="right")
    cl, bc = v["classes"], v["by_class"]
    for e in ECOSYSTEMS:
        t.add_row(e, *[f"{cl[c][e]:,}" for c in ("A", "B", "C")])
    t.add_section()
    t.add_row("Repos", *[f"{cl[c]['strongest']:,}" for c in ("A", "B", "C")],
              style="bold")
    for label, key in (("GitHub %", "github"), ("Git %", "git"), ("Valid %", "valid")):
        t.add_row(label, *[_pct(bc[c][key], bc[c]["repos"]) for c in ("A", "B", "C")])
    console.print(t)

    n = r["scope"]
    t = Table(title=f"Risk — score distribution (scope {n})", header_style="bold dim")
    for col in ("Component / subcomponent", "Column", "Min", "P25", "P50", "P75", "Max"):
        t.add_column(col, justify="right" if col not in
                     ("Component / subcomponent", "Column") else "left")
    for row in r["distribution"]:
        vals = [_fmt(x, row["fmt"]) for x in row["q"]] if row["q"] else ["—"] * 5
        label = f"[bold]{row['label']}[/bold]" if row["is_component"] else f"· {row['label']}"
        cells = [f"[bold]{x}[/bold]" if row["is_component"] else x for x in vals]
        t.add_row(label, f"[dim]{row['column']}[/dim]", *cells)
    console.print(t)

    for name, steps in r["funnels"].items():
        t = Table(title=f"Risk — {name} funnel", header_style="bold dim")
        t.add_column("Step")
        t.add_column("Repos", justify="right")
        t.add_column("%", justify="right")
        t.add_row("input top repos", str(n), "100%")
        for label, cnt in steps:
            t.add_row(label.replace("`", ""), str(cnt), _pct(cnt, n))
        console.print(t)

    t = Table(title="Risk — intent and nonprofit", header_style="bold dim")
    t.add_column("Category")
    t.add_column("Repos", justify="right")
    t.add_column("%", justify="right")
    it, npt = r["intent_count"], r["nonprofit_count"]
    t.add_row("intent — any declared funding signal", str(it), _pct(it, n))
    t.add_row("intent — no declared signal", str(n - it), _pct(n - it, n))
    t.add_row("nonprofit — community / independent", str(npt), _pct(npt, n))
    t.add_row("nonprofit — company-backed (excluded)", str(n - npt), _pct(n - npt, n))
    console.print(t)

    console.print(f"\n[dim]bus-factor-1: {r['bf1_pct']:.1f}% of computed · "
                  f"overall score: {r['overall_scored']}/{n} scored "
                  f"({r['overall_gap']} blank — incomplete, mostly missing workload)[/dim]")


# ── rendering: markdown (stats.md tables) ────────────────────────────────────

def markdown(v: dict, r: dict) -> str:
    out: list[str] = []
    a = out.append
    a("### Repo identity coverage\n")
    a("| Step | A | B | C | Total | Comment |")
    a("|---|--:|--:|--:|--:|---|")
    bc = v["by_class"]

    def _id_row(label: str, d: dict, comment: str, bold: bool = False) -> str:
        cells = [f"{x:,}" for x in (d["A"], d["B"], d["C"], d["A"] + d["B"] + d["C"])]
        if bold:
            return f"| **{label}** | " + " | ".join(f"**{x}**" for x in cells) + f" | {comment} |"
        return f"| {label} | " + " | ".join(cells) + f" | {comment} |"

    a(_id_row("Packages", v["pkg_class"], "package universe (after dep tree)"))
    a(_id_row("GitHub total repos", v["ght_class"], "package appearances in a github group"))
    a(_id_row("GitHub unique repos", {c: bc[c]["github"] for c in "ABC"},
              f"deduped; + {v['orphan']:,} orphans = {v['rows']:,} repos"))
    a(_id_row("Valid repos", {c: bc[c]["valid"] for c in "ABC"},
              "github repo resolves (200); incl. archived mirrors", bold=True))

    a("\n### Repo class distribution\n")
    a("| Metric | A | B | C |")
    a("|---|--:|--:|--:|")
    cl = v["classes"]
    for e in ECOSYSTEMS:
        a(f"| {e} | {cl['A'][e]:,} | {cl['B'][e]:,} | {cl['C'][e]:,} |")
    a(f"| **Repos** | **{cl['A']['strongest']:,}** | "
      f"**{cl['B']['strongest']:,}** | **{cl['C']['strongest']:,}** |")
    bc = v["by_class"]
    for label, key in (("GitHub %", "github"), ("Git %", "git"), ("Valid %", "valid")):
        a(f"| {label} | " + " | ".join(
            _pct(bc[c][key], bc[c]["repos"]) for c in ("A", "B", "C")) + " |")

    n = r["scope"]
    a(f"\n### Score distribution by component (scope {n})\n")
    a("| Component / subcomponent | Column | Min | P25 | P50 | P75 | Max |")
    a("|---|---|--:|--:|--:|--:|--:|")
    for row in r["distribution"]:
        vals = [_fmt(x, row["fmt"]) for x in row["q"]] if row["q"] else [""] * 5
        if row["is_component"]:
            a(f"| **{row['label']}** | `{row['column']}` | " +
              " | ".join(f"**{x}**" for x in vals) + " |")
        else:
            a(f"| · {row['label']} | `{row['column']}` | " + " | ".join(vals) + " |")

    for name, steps in r["funnels"].items():
        a(f"\n### {name.capitalize()} funnel\n")
        a("| Step | Repos | % |")
        a("|---|---:|---:|")
        a(f"| input top repos | {n} | 100% |")
        for label, cnt in steps:
            mark = "**" if label.endswith(" score") else ""
            a(f"| {mark}{label}{mark} | {mark}{cnt}{mark} | {mark}{_pct(cnt, n)}{mark} |")

    it, npt = r["intent_count"], r["nonprofit_count"]
    a("\n### Intent and nonprofit\n")
    a("| Category | Repos | % |")
    a("|---|---:|---:|")
    a(f"| intent — any declared funding signal | {it} | {_pct(it, n)} |")
    a(f"| intent — no declared signal | {n - it} | {_pct(n - it, n)} |")
    a(f"| nonprofit — community / independent | {npt} | {_pct(npt, n)} |")
    a(f"| nonprofit — company-backed (excluded) | {n - npt} | {_pct(n - npt, n)} |")
    return "\n".join(out)


# ── drift check ──────────────────────────────────────────────────────────────

def check(v: dict, r: dict) -> int:
    """Verify docs/stats.md is current; exit 1 on drift.

    Every generated table DATA row (any `| … |` line carrying a digit — headers
    and separators have none) must appear verbatim in stats.md. This is a full
    match, not a loose substring (a stale `817` can't hide behind another `817`),
    over every figure the generator owns. Static tables (the per-eco value funnel)
    are not generated here, so they are not checked.
    """
    text = (ROOT / "docs" / "stats.md").read_text(encoding="utf-8")
    rows = [ln.strip() for ln in markdown(v, r).splitlines()
            if ln.strip().startswith("|") and any(ch.isdigit() for ch in ln)]
    missing = [ln for ln in rows if ln not in text]
    if missing:
        console.print(f"[red]docs/stats.md is STALE — {len(missing)} row(s) drifted:[/red]")
        for ln in missing:
            console.print(f"  {ln}")
        console.print("[dim]refresh: uv run python scripts/stats.py --markdown[/dim]")
        return 1
    console.print(f"[green]docs/stats.md current — all {len(rows)} data rows match.[/green]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute docs/stats.md figures.")
    ap.add_argument("--markdown", action="store_true", help="emit stats.md tables")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if docs/stats.md headline numbers drift")
    args = ap.parse_args()

    v, r = value_stats(), risk_stats()
    if args.check:
        return check(v, r)
    if args.markdown:
        print(markdown(v, r))
        return 0
    dashboard(v, r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
