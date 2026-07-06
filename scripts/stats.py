"""Generate every pipeline statistic shown on the preview workbook's stats sheet.

The single source of truth for the model's counts, funnels, coverage and
distribution figures. Everything is recomputed from the live CSVs:

  uv run python scripts/stats.py              # rich dashboard (read the numbers)
  uv run python scripts/stats.py --markdown   # emit the tables as markdown

`src.build_preview_workbook` imports this module and renders `markdown()`
straight onto the `stats` sheet of data/preview/preview.xlsx — there is no
intermediate stats document to refresh or drift.

Every figure is derived from data, never hard-coded:
  - Value tables       ← data/value/value.csv (+ data/value/stats.csv
                         top-of-funnel, per-eco data/sources/<eco>/results.csv).
  - Risk tables        ← data/risk/*.csv scoped to `load_top_repos()` (the top
                         repos), plus the source CSVs each funnel step reads.
  - Eligibility tables ← data/eligibility/*.csv scoped to
                         `load_top_repos(skip_archived=False)` (top repos
                         INCLUDING archived — they surface as active=False).

The Risk denominator is the top-repo set (`load_top_repos()`), matching what the
builders actually score; a blank score is therefore a real coverage gap, except
where the metric legitimately doesn't exist.
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


def _is_github(r: dict) -> bool:
    """A value.csv row whose repo lives on GitHub (platform == github)."""
    return (r.get("platform") or "").strip().lower() == "github" and _present(r.get("repo"))


def value_stats() -> dict:
    rows = _load("data/value/value.csv")
    arch = _archived_map()
    m = len(rows)
    gh = sum(1 for r in rows if _is_github(r))
    git = sum(1 for r in rows if _present(r.get("git_url")))
    # git_valid is the renamed column (was `valid`); fall back for pre-rename CSVs.
    valid = sum(1 for r in rows if _truthy(r.get("git_valid")))
    orphan = m - gh

    # git-URL breakdown: host family (from `platform`) and validity, over the
    # rows that carry a git URL at all.
    with_url = [r for r in rows if _present(r.get("git_url"))]
    git_urls = {
        "total": len(with_url),
        "github": sum(1 for r in with_url if (r.get("platform") or "") == "github"),
        "gitlab": sum(1 for r in with_url if (r.get("platform") or "") == "gitlab"),
        "valid": sum(1 for r in with_url if _truthy(r.get("git_valid"))),
    }
    git_urls["others"] = git_urls["total"] - git_urls["github"] - git_urls["gitlab"]
    git_urls["invalid"] = git_urls["total"] - git_urls["valid"]

    def _is_active(r: dict) -> bool:
        if not _is_github(r):
            return False
        s = (r.get("repo") or "").strip().lower()
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
            "github": sum(1 for r in sub if _is_github(r)),
            "active": sum(1 for r in sub if _is_active(r)),
            "git": sum(1 for r in sub if _present(r.get("git_url"))),
            "valid": sum(1 for r in sub if _truthy(r.get("git_valid"))),
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
        if _is_github(r):
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
        "git_urls": git_urls,
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
    o_scored, o_q = col_dist(risk, "risk_score")
    distribution.append({"label": "Overall", "column": "risk_score",
                         "is_component": True, "fmt": "int",
                         "scored": o_scored, "q": o_q})

    # per-dimension funnels
    funnels = {name: [(label, _count(dims[name], scope, kind, col))
                      for label, kind, col in steps]
               for name, steps in RISK_FUNNELS.items()}

    # headline prose figures
    conc = dims["concentration"]
    bf_vals = [_num(r.get("bf_commits_git_5y")) for r in conc if r["repo"] in scope]
    bf_computed = [v for v in bf_vals if v is not None]
    bf1 = sum(1 for v in bf_computed if v == 1)

    return {
        "scope": n, "distribution": distribution, "funnels": funnels,
        "overall_scored": o_scored, "overall_gap": n - o_scored,
        "bf1_pct": 100 * bf1 / len(bf_computed) if bf_computed else 0,
    }


# ── eligibility ──────────────────────────────────────────────────────────────

ELIGIBILITY_FLAGS = ("oss", "intent", "nonprofit", "active")


def eligibility_scope_set() -> set[str]:
    """The eligibility denominator — top repos INCLUDING archived ones.

    Wider than the risk scope: archived repos stay in so the stage can mark
    them active=False instead of silently dropping them.
    """
    return {e.repo for e in load_top_repos(skip_archived=False) if e.repo}


def eligibility_stats() -> dict:
    scope = eligibility_scope_set()
    n = len(scope)
    lic = [r for r in _load("data/eligibility/licenses.csv") if r["repo"] in scope]
    act = [r for r in _load("data/eligibility/active.csv") if r["repo"] in scope]
    fund = _load("data/eligibility/funding.csv")
    elig = [r for r in _load("data/eligibility/eligibility.csv") if r["repo"] in scope]

    licenses = {
        "resolved": sum(1 for r in lic if _present(r.get("license"))),
        "override": sum(1 for r in lic if r.get("license_source") == "override"),
        "registry": sum(1 for r in lic if r.get("license_source") == "registry"),
        "github": sum(1 for r in lic if r.get("license_source") == "github"),
        "gitlab": sum(1 for r in lic if r.get("license_source") == "gitlab"),
        "oss_true": sum(1 for r in lic if r.get("oss") == "True"),
        "oss_false": sum(1 for r in lic if r.get("oss") == "False"),
        "oss_unknown": sum(1 for r in lic if (r.get("oss") or "").strip() == ""),
    }
    active = {
        "eol": sum(1 for r in act if _truthy(r.get("eol"))),
        "archived": sum(1 for r in act if _truthy(r.get("archived"))),
        "mirror": sum(1 for r in act
                      if _truthy(r.get("archived")) and _truthy(r.get("mirror"))),
        "active": sum(1 for r in act if _truthy(r.get("active"))),
    }
    intent_count = _count(fund, scope, "bool", "intent")
    nonprofit_count = _count(fund, scope, "bool", "nonprofit")

    rollup = {f: sum(1 for r in elig if _truthy(r.get(f)))
              for f in ELIGIBILITY_FLAGS}
    rollup["eligible"] = sum(1 for r in elig if _truthy(r.get("eligible")))
    # repos failing ONLY this flag — what fixing it alone would unlock
    sole = {f: sum(1 for r in elig
                   if not _truthy(r.get(f))
                   and all(_truthy(r.get(g)) for g in ELIGIBILITY_FLAGS if g != f))
            for f in ELIGIBILITY_FLAGS}

    return {"scope": n, "licenses": licenses, "active": active,
            "intent_count": intent_count, "nonprofit_count": nonprofit_count,
            "rollup": rollup, "sole": sole}


# ── end-to-end funnel ────────────────────────────────────────────────────────

def funnel_stats(v: dict, r: dict, e: dict) -> list[tuple[str, int, int, str, str]]:
    """The whole pipeline as one funnel: package universe → priority-ranked
    preview rows. Each row = (stage, count, denominator, denominator label,
    comment); the pre-scope stages narrow against the previous stage, the
    post-scope stages are parallel filters over the top-repo set (so each is
    expressed against `top repos`, not the row above)."""
    preview = _load("data/preview/repos.csv")
    bc = v["by_class"]
    pkgs = sum(v["pkg_class"][c] for c in "ABC")
    valid = sum(bc[c]["valid"] for c in "ABC")
    class_a = v["classes"]["A"]["strongest"]
    top = e["scope"]
    value_scored = sum(1 for p in preview if _present(p.get("value_score")))
    scored = sum(1 for p in preview if _present(p.get("score")))
    ranked = sum(1 for p in preview if _present(p.get("priority")))
    return [
        ("packages (after dep tree)", pkgs, 0, "",
         "package universe across the four ecosystems"),
        ("distinct repos", v["rows"], pkgs, "packages",
         "package→repo union (any host), incl. url-less orphans"),
        ("valid repos", valid, v["rows"], "repos",
         "upstream resolves — GitHub/GitLab API or git ls-remote"),
        ("class-A repos", class_a, valid, "valid",
         "strongest class = A (≤75% cumulative PageRank share)"),
        ("top repos", top, class_a, "class A",
         "valid class-A on GitHub+GitLab, archived included — the risk/eligibility scope"),
        ("value_score present", value_scored, top, "top repos",
         "≥2 value components (openssf_crit / eco_crit / top_eco_pct)"),
        ("risk_score present", r["overall_scored"], top, "top repos",
         "all four risk dimensions scored"),
        ("fully scored", scored, top, "top repos",
         "value_score × risk_score → preview `score`"),
        ("eligible", e["rollup"]["eligible"], top, "top repos",
         "oss AND intent AND nonprofit AND active"),
        ("priority-ranked", ranked, top, "top repos",
         "eligible AND fully scored → preview `priority`"),
    ]


# ── rendering: rich dashboard ────────────────────────────────────────────────

def _pct(n: int, d: int) -> str:
    if not d:
        return "—"
    p = 100 * n / d
    # the preview stats sheet convention: an exact 100% is written "100%", everything else 1dp.
    return "100%" if n == d else f"{p:.1f}%"


def dashboard(v: dict, r: dict, e: dict) -> None:
    console.rule("[bold]Pipeline statistics[/bold]")
    console.print(f"value.csv rows: [bold]{v['rows']:,}[/bold]   "
                  f"risk scope (top repos): [bold]{r['scope']:,}[/bold]\n")

    t = Table(title="End-to-end funnel", header_style="bold dim")
    t.add_column("Stage")
    t.add_column("Count", justify="right")
    t.add_column("%", justify="right")
    t.add_column("of")
    for stage, cnt, denom, denom_label, _comment in funnel_stats(v, r, e):
        t.add_row(stage, f"{cnt:,}", _pct(cnt, denom) if denom else "—",
                  denom_label or "—",
                  style="bold" if stage in ("top repos", "eligible") else None)
    console.print(t)

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
    for eco in ECOSYSTEMS:
        t.add_row(eco, *[f"{cl[c][eco]:,}" for c in ("A", "B", "C")])
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

    console.print(f"\n[dim]bus-factor-1: {r['bf1_pct']:.1f}% of computed · "
                  f"overall score: {r['overall_scored']}/{n} scored "
                  f"({r['overall_gap']} blank — incomplete, mostly missing workload)[/dim]")

    ne = e["scope"]
    lic, act = e["licenses"], e["active"]
    t = Table(title=f"Eligibility — licenses (scope {ne})", header_style="bold dim")
    t.add_column("Step")
    t.add_column("Repos", justify="right")
    t.add_column("%", justify="right")
    for label, cnt in (("license resolved", lic["resolved"]),
                       ("· from override", lic["override"]),
                       ("· from registry", lic["registry"]),
                       ("· from GitHub", lic["github"]),
                       ("· from GitLab", lic["gitlab"]),
                       ("oss=True (OSS-approved)", lic["oss_true"]),
                       ("oss=False (known non-OSS)", lic["oss_false"]),
                       ("oss unknown (no signal)", lic["oss_unknown"])):
        t.add_row(label, str(cnt), _pct(cnt, ne))
    console.print(t)

    t = Table(title="Eligibility — activity", header_style="bold dim")
    t.add_column("Category")
    t.add_column("Repos", justify="right")
    t.add_column("%", justify="right")
    for label, cnt in (("eol (override)", act["eol"]),
                       ("archived", act["archived"]),
                       ("archived but mirror-exempt", act["mirror"]),
                       ("active", act["active"])):
        t.add_row(label, str(cnt), _pct(cnt, ne))
    console.print(t)

    t = Table(title="Eligibility — intent and nonprofit", header_style="bold dim")
    t.add_column("Category")
    t.add_column("Repos", justify="right")
    t.add_column("%", justify="right")
    it, npt = e["intent_count"], e["nonprofit_count"]
    t.add_row("intent — any funding signal", str(it), _pct(it, ne))
    t.add_row("intent — no funding signal", str(ne - it), _pct(ne - it, ne))
    t.add_row("nonprofit — community / independent", str(npt), _pct(npt, ne))
    t.add_row("nonprofit — company-backed", str(ne - npt), _pct(ne - npt, ne))
    console.print(t)

    t = Table(title="Eligibility — rollup", header_style="bold dim")
    t.add_column("Check")
    t.add_column("True", justify="right")
    t.add_column("%", justify="right")
    t.add_column("sole blocker", justify="right")
    for f in ELIGIBILITY_FLAGS:
        t.add_row(f, str(e["rollup"][f]), _pct(e["rollup"][f], ne), str(e["sole"][f]))
    t.add_section()
    t.add_row("[bold]eligible[/bold]", f"[bold]{e['rollup']['eligible']}[/bold]",
              f"[bold]{_pct(e['rollup']['eligible'], ne)}[/bold]", "")
    console.print(t)


# ── rendering: markdown (the preview stats sheet tables) ────────────────────────────────────

def markdown(v: dict, r: dict, e: dict) -> str:
    out: list[str] = []
    a = out.append

    a("### End-to-end funnel\n")
    a("| Stage | Count | % |")
    a("|---|--:|--:|")
    for stage, cnt, denom, denom_label, comment in funnel_stats(v, r, e):
        pct = _pct(cnt, denom) if denom else ""
        # one self-describing label: stage — comment (% is of <denominator>)
        label = f"{stage} — {comment}" + (f" (% of {denom_label})" if denom else "")
        mark = "**" if stage in ("top repos", "eligible") else ""
        a(f"| {mark}{label}{mark} | {mark}{cnt:,}{mark} | {mark}{pct}{mark} |")
    a("")

    a("### Per-ecosystem value funnel\n")
    a("| Metric | npm | pypi | crates | cpp | Total |")
    a("|---|--:|--:|--:|--:|--:|")
    fu = v["funnel"]

    def _eco_row(label: str, key: str) -> str:
        vals = [fu[eco][key] for eco in ECOSYSTEMS]
        return (f"| {label} | " + " | ".join(f"{x:,}" for x in vals)
                + f" | {sum(vals):,} |")

    def _eco_pct_row(label: str, num_key: str, den_key: str) -> str:
        cells = [_pct(fu[eco][num_key], fu[eco][den_key]) for eco in ECOSYSTEMS]
        tot_n = sum(fu[eco][num_key] for eco in ECOSYSTEMS)
        tot_d = sum(fu[eco][den_key] for eco in ECOSYSTEMS)
        return f"| {label} | " + " | ".join(cells) + f" | {_pct(tot_n, tot_d)} |"

    a(_eco_row("top packages — 95% of downloads", "top"))
    a(_eco_row("after dep tree — + transitive deps", "deps"))
    a(_eco_row("with git URL — any host", "git"))
    a(_eco_row("with GitHub repo", "github"))
    a(_eco_pct_row("git URL % — of dep-tree packages", "git", "results"))
    a(_eco_pct_row("GitHub % — of dep-tree packages", "github", "results"))
    a("")

    a("### Repo identity coverage\n")
    a("| Step | A | B | C | Total |")
    a("|---|--:|--:|--:|--:|")
    bc = v["by_class"]

    def _id_row(label: str, d: dict, comment: str, bold: bool = False) -> str:
        cells = [f"{x:,}" for x in (d["A"], d["B"], d["C"], d["A"] + d["B"] + d["C"])]
        full = f"{label} — {comment}"
        if bold:
            return f"| **{full}** | " + " | ".join(f"**{x}**" for x in cells) + " |"
        return f"| {full} | " + " | ".join(cells) + " |"

    a(_id_row("Packages", v["pkg_class"], "package universe (after dep tree)"))
    a(_id_row("GitHub total repos", v["ght_class"], "package appearances in a github group"))
    a(_id_row("GitHub unique repos", {c: bc[c]["github"] for c in "ABC"},
              f"deduped; + {v['orphan']:,} orphans = {v['rows']:,} repos"))
    a(_id_row("Valid repos", {c: bc[c]["valid"] for c in "ABC"},
              "upstream resolves — github/gitlab API or non-github ls-remote; "
              "incl. archived mirrors", bold=True))

    a("\n### Git URLs\n")
    a("| Git URL | Repos | % |")
    a("|---|--:|--:|")
    gu = v["git_urls"]
    a(f"| on GitHub | {gu['github']:,} | {_pct(gu['github'], gu['total'])} |")
    a(f"| on GitLab | {gu['gitlab']:,} | {_pct(gu['gitlab'], gu['total'])} |")
    a(f"| on other hosts | {gu['others']:,} | {_pct(gu['others'], gu['total'])} |")
    a(f"| **valid — upstream resolves** | **{gu['valid']:,}** "
      f"| **{_pct(gu['valid'], gu['total'])}** |")
    a(f"| invalid — unreachable | {gu['invalid']:,} | {_pct(gu['invalid'], gu['total'])} |")
    a(f"| total with a git URL | {gu['total']:,} | 100% |")

    a("\n### Repo class distribution\n")
    a("| Metric | A | B | C |")
    a("|---|--:|--:|--:|")
    cl = v["classes"]
    for eco in ECOSYSTEMS:
        a(f"| {eco} | {cl['A'][eco]:,} | {cl['B'][eco]:,} | {cl['C'][eco]:,} |")
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

    # ── Eligibility sections ──
    ne = e["scope"]
    lic, act = e["licenses"], e["active"]
    a(f"\n### Licenses (scope {ne})\n")
    a("| Step | Repos | % |")
    a("|---|---:|---:|")
    a(f"| input top repos (incl. archived) | {ne} | 100% |")
    for label, cnt in (("license resolved", lic["resolved"]),
                       ("· from override", lic["override"]),
                       ("· from registry", lic["registry"]),
                       ("· from GitHub", lic["github"]),
                       ("· from GitLab", lic["gitlab"]),
                       ("**oss=True (OSS-approved)**", lic["oss_true"]),
                       ("oss=False (known non-OSS)", lic["oss_false"]),
                       ("oss unknown (no signal)", lic["oss_unknown"])):
        mark = "**" if label.startswith("**") else ""
        clean = label.strip("*")
        a(f"| {mark}{clean}{mark} | {mark}{cnt}{mark} | {mark}{_pct(cnt, ne)}{mark} |")

    a("\n### Activity\n")
    a("| Category | Repos | % |")
    a("|---|---:|---:|")
    for label, cnt in (("eol (override)", act["eol"]),
                       ("archived", act["archived"]),
                       ("archived but mirror-exempt", act["mirror"]),
                       ("**active**", act["active"])):
        mark = "**" if label.startswith("**") else ""
        clean = label.strip("*")
        a(f"| {mark}{clean}{mark} | {mark}{cnt}{mark} | {mark}{_pct(cnt, ne)}{mark} |")

    it, npt = e["intent_count"], e["nonprofit_count"]
    a("\n### Intent and nonprofit\n")
    a("| Category | Repos | % |")
    a("|---|---:|---:|")
    a(f"| intent — any funding signal | {it} | {_pct(it, ne)} |")
    a(f"| intent — no funding signal | {ne - it} | {_pct(ne - it, ne)} |")
    a(f"| nonprofit — community / independent | {npt} | {_pct(npt, ne)} |")
    a(f"| nonprofit — company-backed | {ne - npt} | {_pct(ne - npt, ne)} |")

    a("\n### Eligibility rollup\n")
    a("| Check | True | % | sole blocker |")
    a("|---|---:|---:|---:|")
    for f in ELIGIBILITY_FLAGS:
        a(f"| {f} | {e['rollup'][f]} | {_pct(e['rollup'][f], ne)} | {e['sole'][f]} |")
    a(f"| **eligible** | **{e['rollup']['eligible']}** | "
      f"**{_pct(e['rollup']['eligible'], ne)}** | |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute the pipeline statistics.")
    ap.add_argument("--markdown", action="store_true",
                    help="emit the stats tables as markdown (the preview "
                         "workbook's stats sheet builds from this renderer)")
    args = ap.parse_args()

    v, r, e = value_stats(), risk_stats(), eligibility_stats()
    if args.markdown:
        print(markdown(v, r, e))
        return 0
    dashboard(v, r, e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
