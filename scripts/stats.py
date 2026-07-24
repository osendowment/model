"""Generate every pipeline statistic shown on the preview workbook's pipeline sheet.

The single source of truth for the model's counts, funnels, coverage and
distribution figures. Everything is recomputed from the live CSVs:

  uv run python scripts/stats.py              # rich dashboard (read the numbers)
  uv run python scripts/stats.py --markdown   # emit the tables as markdown

`src.preview.build_preview_workbook` imports this module and renders `markdown()`
straight onto the `pipeline` sheet of data/preview/preview.xlsx — there is no
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
import json
from collections import Counter
import sys
from pathlib import Path

import numpy as np
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.common.lfs import is_lfs_pointer  # noqa: E402
from src.common.params import VALUE_CLASS_A, VALUE_CLASS_B  # noqa: E402
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


def platform_split(scope: set[str]) -> dict[str, int]:
    """{github, gitlab} repo counts within `scope`, from value.csv `platform`.

    The Risk/Eligibility 'Inputs' tables break the shared class-A scope down
    by host. Every scope repo comes from value.csv, so the join is total."""
    out = {"github": 0, "gitlab": 0}
    for r in _load("data/value/value.csv"):
        if r["repo"] in scope:
            p = (r.get("platform") or "").strip().lower()
            if p in out:
                out[p] += 1
    return out


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
    # rows that carry a git URL at all — each bucket split by strongest class.
    with_url = [r for r in rows if _present(r.get("git_url"))]

    def _url_bucket(sub: list[dict]) -> dict:
        by_class = Counter((r.get("class") or "").strip() for r in sub)
        return {"A": by_class["A"], "B": by_class["B"], "C": by_class["C"],
                "total": len(sub)}

    def _eco_url_bucket(eco: str) -> dict:
        """URL-carrying repos that belong to `eco`, classed by their class IN
        that ecosystem (class_npm/…). Multi-eco repos count once per ecosystem
        they belong to — hence the grand total is labelled (unique)."""
        sub = [r for r in with_url
               if eco in (r.get("ecosystems") or "").split(",")]
        by_class = Counter((r.get(CLASS_COL[eco]) or "").strip() for r in sub)
        return {"A": by_class["A"], "B": by_class["B"], "C": by_class["C"],
                "total": len(sub),
                "ghgl": sum(1 for r in sub
                            if (r.get("platform") or "") in ("github", "gitlab"))}

    git_urls = {
        "github": _url_bucket([r for r in with_url if (r.get("platform") or "") == "github"]),
        "gitlab": _url_bucket([r for r in with_url if (r.get("platform") or "") == "gitlab"]),
        "others": _url_bucket([r for r in with_url
                               if (r.get("platform") or "") not in ("github", "gitlab")]),
        "valid": _url_bucket([r for r in with_url if _truthy(r.get("git_valid"))]),
        "invalid": _url_bucket([r for r in with_url if not _truthy(r.get("git_valid"))]),
        "all": _url_bucket(with_url),
        "by_eco": {eco: _eco_url_bucket(eco) for eco in ECOSYSTEMS},
    }

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

    # avg annual downloads (2021-2025) per stats.csv column; cpp = debian+homebrew
    years = [f"downloads_{y}" for y in range(2021, 2026)]
    avg_dl = {}
    for col in ("npm", "pypi", "crates", "debian", "homebrew"):
        vals = [float(smatrix.get(y, {}).get(col) or 0) for y in years]
        avg_dl[col] = sum(vals) / len(years)
    avg_dl["cpp"] = avg_dl["debian"] + avg_dl["homebrew"]

    # all tracked packages: the per-registry universe we measure downloads on
    def _col_set(path: str, col: str) -> set[str]:
        return {r[col] for r in _load(path) if (r.get(col) or "").strip()}

    def _row_count(path: str) -> int:
        """Data-row count via buffered newline count (fast on multi-100MB files;
        safe here — these CSVs never carry embedded newlines)."""
        if is_lfs_pointer(ROOT / path):
            raise SystemExit(
                f"{path} is an unmaterialised git-LFS pointer — the count would "
                f"silently be ~0. Run: git lfs checkout {path}"
            )
        n = 0
        with open(ROOT / path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                n += chunk.count(b"\n")
        return max(0, n - 1)  # header

    deb_names = _col_set("data/sources/debian/raw/cpp-packages.csv", "package")
    brew_names = _col_set("data/sources/homebrew/raw/formulas.csv", "name")
    tracked = {
        # the nice-registry package→repo universe (~2.6M), not just the
        # packages we fetched download counts for
        "npm": _row_count("data/sources/npm/nice-registry/packages.csv"),
        "pypi": len(_col_set("data/sources/pypi/bigquery/bq-package-downloads.csv",
                             "package")),
        "crates": len(_col_set("data/sources/crates/db-dump/crates.csv", "name")),
        "debian": len(deb_names),
        "homebrew": len(brew_names),
        "cpp": len(deb_names | brew_names),
    }
    top_pkgs = {eco: int(smatrix.get("packages_top", {}).get(eco, 0) or 0)
                for eco in ("npm", "pypi", "crates", "debian", "homebrew", "cpp")}

    return {
        "rows": m, "github": gh, "git": git, "valid": valid, "orphan": orphan,
        "git_urls": git_urls, "avg_dl": avg_dl, "tracked": tracked,
        "top_pkgs": top_pkgs,
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
        ("Bus Factor (5 years, git contributors)", "bf_commits_git_5y", "int"),
        ("HHI (5 years, git contributors)", "hhi_commits_git_5y", "int")]),
    ("Complexity", "complexity", "score", [
        ("Lines of Code (last year snapshot)", "loc_eoy", "int"),
        ("Cyclomatic Complexity (last year snapshot)", "cyclomatic_max", "int")]),
    ("Security", "security", "score", [
        ("OpenSSF Security Score (most recent data)", "openssf_score", "f1"),
        ("CVE count (5 years)", "cve_count_5y", "int")]),
    ("Workload", "workload", "score", [
        ("LOCs / active contributors (5 years)", "loc_per_ac", "int"),
        ("CVE / active contributors (5 years)", "cve_per_ac", "f2"),
        ("Net new issues / active contributors (5 years)", "nni_per_ac", "f2")]),
]

# (dimension CSV, [(label, kind, column/expr)]) for each per-dimension funnel.
# kind: "present" = non-blank; "gt0" = numeric > 0; "eq" = numeric == 0/1; "bool".
RISK_FUNNELS = {
    "concentration": [("bus factor / HHI (git 5y) computed", "present", "bf_commits_git_5y_p"),
                      ("bus factor / HHI (git full) computed", "present", "bf_commits_git_full_p"),
                      ("Concentration score", "present", "score")],
    "complexity": [("lines of code (scc)", "present", "loc_eoy"),
                   ("cyclomatic max (lizard)", "present", "cyclomatic_max"),
                   ("cognitive max (lizard)", "present", "cognitive_max"),
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
    distribution.append({"label": "Risk Score", "column": "risk_score",
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
        "platform": platform_split(scope),
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
        "active": sum(1 for r in act if _truthy(r.get("active"))),
    }
    intent_count = _count(fund, scope, "bool", "intent")
    nonprofit_count = _count(fund, scope, "bool", "nonprofit")

    # intent signal breakdown — how many scope repos carry each funding signal
    # (a repo can carry several; these are NOT mutually exclusive, so they do
    # not sum to the intent total). oc_slug / host are presence, not booleans.
    fund_scope = [r for r in fund if r["repo"] in scope]

    def _sig(col: str) -> int:
        return sum(1 for r in fund_scope if _truthy(r.get(col)))

    def _present_sig(col: str) -> int:
        return sum(1 for r in fund_scope if _present(r.get(col)))

    intent_signals = {
        "gh_sponsors": _sig("gh_sponsors_enabled"),
        "funding_yml": _sig("has_funding_yml"),
        "funding_json": _sig("has_funding_json"),
        "pkg_funding": sum(1 for r in fund_scope
                           if _truthy(r.get("has_npm_funding"))
                           or _truthy(r.get("has_pypi_funding"))),
        "maintainer_sponsors": _sig("bf_maintainer_fundable"),
        "open_collective": _present_sig("oc_slug"),
        "institutional_host": _present_sig("host"),
    }

    rollup = {f: sum(1 for r in elig if _truthy(r.get(f)))
              for f in ELIGIBILITY_FLAGS}
    rollup["eligible"] = sum(1 for r in elig if _truthy(r.get("eligible")))
    # repos failing ONLY this flag — what fixing it alone would unlock
    sole = {f: sum(1 for r in elig
                   if not _truthy(r.get(f))
                   and all(_truthy(r.get(g)) for g in ELIGIBILITY_FLAGS if g != f))
            for f in ELIGIBILITY_FLAGS}

    # ── Distribution sub-rows: how each source / signal / reason splits its
    # own population on the parent check (yes = pass, no = fail), plus a
    # `solo` = the count's contribution to the check's SOLE-blocker total —
    # repos this sub-reason makes ineligible where its check is the only
    # failing one. So the sub-row `solo`s PARTITION the check's solo blocker
    # (sum of subs == component). Intent's sub-rows are the signals that GRANT
    # intent; a present signal never blocks, so they read 0 — an intent block
    # is the absence of every signal, attributable to no single one.
    elig_by_repo = {r["repo"]: r for r in elig}
    lic_by_repo = {r["repo"]: r for r in lic}
    act_by_repo = {r["repo"]: r for r in act}

    # sole-blocker repos per check: this check fails and every other passes.
    sole_repos = {
        f: {r["repo"] for r in elig
            if not _truthy(r.get(f))
            and all(_truthy(r.get(g)) for g in ELIGIBILITY_FLAGS if g != f)}
        for f in ELIGIBILITY_FLAGS
    }

    def _oss(repo: str) -> bool:
        return elig_by_repo.get(repo, {}).get("oss") == "True"

    def _lic_source(src: str) -> dict:
        sub = [r for r in lic if r.get("license_source") == src]
        yes = sum(1 for r in sub if _oss(r["repo"]))
        # solo: sole-oss-blockers whose (non-OSS) license came from this source.
        solo = sum(1 for repo in sole_repos["oss"]
                   if lic_by_repo.get(repo, {}).get("license_source") == src)
        return {"count": len(sub), "yes": yes, "no": len(sub) - yes, "solo": solo}

    def _active_reason(pred, cause: str) -> dict:
        sub = [r for r in act if pred(r)]
        yes = sum(1 for r in sub if _truthy(r.get("active")))
        if cause == "eol":
            solo = sum(1 for repo in sole_repos["active"]
                       if _truthy(act_by_repo.get(repo, {}).get("eol")))
        else:  # archived (no exemption): archived and not eol blocks
            solo = sum(1 for repo in sole_repos["active"]
                       if not _truthy(act_by_repo.get(repo, {}).get("eol"))
                       and _truthy(act_by_repo.get(repo, {}).get("archived")))
        return {"count": len(sub), "yes": yes, "no": len(sub) - yes, "solo": solo}

    SIGNAL_PREDS = {
        "gh_sponsors": lambda r: _truthy(r.get("gh_sponsors_enabled")),
        "funding_yml": lambda r: _truthy(r.get("has_funding_yml")),
        "funding_json": lambda r: _truthy(r.get("has_funding_json")),
        "pkg_funding": lambda r: (_truthy(r.get("has_npm_funding"))
                                  or _truthy(r.get("has_pypi_funding"))),
        "maintainer": lambda r: _truthy(r.get("bf_maintainer_fundable")),
        "open_collective": lambda r: _present(r.get("oc_slug")),
        "institutional_host": lambda r: _present(r.get("host")),
    }

    def _intent_signal_row(key: str) -> dict:
        sub = [r for r in fund_scope if SIGNAL_PREDS[key](r)]
        yes = sum(1 for r in sub
                  if _truthy(elig_by_repo.get(r["repo"], {}).get("intent")))
        # a present funding signal grants intent; it never blocks eligibility.
        return {"count": len(sub), "yes": yes, "no": len(sub) - yes, "solo": 0}

    company = sum(1 for r in elig if not _truthy(r.get("nonprofit")))
    community = rollup["nonprofit"]
    dist_subs = {
        "oss": [
            ("License from manual override", _lic_source("override")),
            ("License from package registry", _lic_source("registry")),
            ("License from GitHub repo", _lic_source("github")),
            ("License from GitLab repo", _lic_source("gitlab")),
        ],
        "active": [
            ("EOL (manual override)", _active_reason(
                lambda r: _truthy(r.get("eol")), cause="eol")),
            ("Archived GitHub repo", _active_reason(
                lambda r: _truthy(r.get("archived")), cause="archived")),
        ],
        "intent": [
            ("GitHub Sponsors (owner or repo)", _intent_signal_row("gh_sponsors")),
            ("FUNDING.yml", _intent_signal_row("funding_yml")),
            ("funding.json", _intent_signal_row("funding_json")),
            ("npm / PyPI funding field", _intent_signal_row("pkg_funding")),
            ("GitHub Sponsors (maintainer)", _intent_signal_row("maintainer")),
            ("Open Collective", _intent_signal_row("open_collective")),
            ("Institutional host / owner", _intent_signal_row("institutional_host")),
        ],
        "nonprofit": [
            ("Company-backed",
             {"count": company, "yes": 0, "no": company,
              "solo": len(sole_repos["nonprofit"])}),
            ("Community / independent",
             {"count": community, "yes": community, "no": 0, "solo": 0}),
        ],
    }
    # sort each component's sub-rows by count descending.
    dist_subs = {flag: sorted(rows, key=lambda lr: -lr[1]["count"])
                 for flag, rows in dist_subs.items()}

    return {"scope": n, "licenses": licenses, "active": active,
            "intent_count": intent_count, "nonprofit_count": nonprofit_count,
            "intent_signals": intent_signals, "platform": platform_split(scope),
            "rollup": rollup, "sole": sole, "dist_subs": dist_subs}


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
    # the preview pipeline sheet convention: an exact 100% is written "100%", everything else 1dp.
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


# ── rendering: markdown (the preview pipeline sheet tables) ────────────────────────────────────

def markdown(v: dict, r: dict, e: dict) -> str:
    out: list[str] = []
    a = out.append

    a("## Stage 1: Value\n")

    fu = v["funnel"]
    gu = v["git_urls"]
    dl = v["avg_dl"]
    tr = v["tracked"]
    tp = v["top_pkgs"]

    a("| Funnel step | npm | pypi | crates | cpp | Total |")
    a("|---|--:|--:|--:|--:|--:|")
    a("| Avg annual downloads (2021-2025) | "
      + " | ".join(f"{int(dl[eco]):,}" for eco in ECOSYSTEMS)
      + f" | {int(sum(dl[eco] for eco in ECOSYSTEMS)):,} |")
    a("| All tracked packages | "
      + " | ".join(f"{tr[eco]:,}" for eco in ECOSYSTEMS)
      + f" | {sum(tr[eco] for eco in ECOSYSTEMS):,} |")

    def _eco_row(label: str, key: str) -> str:
        vals = [fu[eco][key] for eco in ECOSYSTEMS]
        return (f"| {label} | " + " | ".join(f"{x:,}" for x in vals)
                + f" | {sum(vals):,} |")

    a(_eco_row("Top packages representing 95% of downloads (2021-2025)", "top"))
    a(_eco_row("Target packages (top + their dependency tree)", "deps"))
    a(_eco_row("Targets with a git URL", "git"))
    uniq = [gu["by_eco"][eco]["total"] for eco in ECOSYSTEMS]
    a("| ^**Unique repo URLs** | " + " | ".join(f"**{x:,}**" for x in uniq)
      + f" | **{gu['all']['total']:,}** |")
    a("")

    # the cpp column decomposed by sub-source, rendered BESIDE the funnel table
    a("<!-- beside -->")
    a("| CPP ecosystem | Homebrew | Debian | Total | Unique |")
    a("|---|--:|--:|--:|--:|")
    a(f"| Avg annual downloads (2021-2025) | {int(dl['homebrew']):,} "
      f"| {int(dl['debian']):,} | {int(dl['cpp']):,} | |")
    a(f"| All tracked packages | {tr['homebrew']:,} | {tr['debian']:,} "
      f"| {tr['homebrew'] + tr['debian']:,} | {tr['cpp']:,} |")
    a(f"| Top packages | {tp['homebrew']:,} | {tp['debian']:,} "
      f"| {tp['homebrew'] + tp['debian']:,} | {tp['cpp']:,} |")
    a("")

    a("| Repo types | Min cum PR pct | Max cum PR pct |")
    a("|---|--:|--:|")
    a(f"| Class A - core representing {int(VALUE_CLASS_A * 100)}% of "
      f"downloads-weighted PageRank | 0 | {VALUE_CLASS_A} |")
    a(f"| Class B - next {int(round((VALUE_CLASS_B - VALUE_CLASS_A) * 100))}% of the "
      f"ecosystem value | {VALUE_CLASS_A} | {VALUE_CLASS_B} |")
    a(f"| Class C - long tail of packages/repos | {VALUE_CLASS_B} | 1 |")
    a("")

    a("| Outputs | Class A | Class B | Class C | Total | % Total |")
    a("|---|--:|--:|--:|--:|--:|")
    grand = gu["all"]["total"]

    def _gu_row(label: str, key: str, bold: bool = False) -> str:
        b = gu[key]
        cells = [f"{b['A']:,}", f"{b['B']:,}", f"{b['C']:,}", f"{b['total']:,}",
                 _pct(b["total"], grand)]
        marker = "^" if label.startswith("^") else ""
        plain = label.lstrip("^")
        if bold:
            return (f"| {marker}**{plain}** | "
                    + " | ".join(f"**{x}**" for x in cells) + " |")
        return f"| {marker}{plain} | " + " | ".join(cells) + " |"

    a(_gu_row("GitHub repos", "github"))
    a(_gu_row("GitLab repos", "gitlab"))
    a(_gu_row("Other repos", "others"))
    a(_gu_row("^Valid git URL (resolved)", "valid"))
    a(_gu_row("Invalid git URL (unreachable)", "invalid"))
    for i, eco in enumerate(ECOSYSTEMS):
        b = gu["by_eco"][eco]
        cells = [f"{b['A']:,}", f"{b['B']:,}", f"{b['C']:,}", f"{b['total']:,}",
                 _pct(b["total"], grand)]
        a(f"| {'^' if i == 0 else ''}{eco} | " + " | ".join(cells) + " |")
    a(_gu_row("^Unique repo URLs", "all", bold=True))

    a("\n## Stage 2: Risk\n")

    n = r["scope"]
    rp = r["platform"]

    # Inputs — the shared class-A scope, split by host.
    a("| Inputs | Count |")
    a("|---|--:|")
    a(f"| GitHub repos (Class A) | {rp['github']:,} |")
    a(f"| GitLab repos (Class A) | {rp['gitlab']:,} |")
    a(f"| ^**Total repos** | **{n:,}** |")
    a("")

    # Outputs — how many scope repos have each score / raw metric computed.
    # The last distribution row is the overall Risk Score (a summary → `^`).
    last = len(r["distribution"]) - 1
    a("| Outputs | Count |")
    a("|---|--:|")
    for i, row in enumerate(r["distribution"]):
        mark = "^" if i == last else ""
        if row["is_component"]:
            a(f"| {mark}**{row['label']}** | **{row['scored']:,}** |")
        else:
            a(f"| · {row['label']} | {row['scored']:,} |")
    a("")

    # Distribution — the score (0–100) and raw-metric quantiles.
    a("| Distribution | Min | P25 | P50 | P75 | Max |")
    a("|---|--:|--:|--:|--:|--:|")
    for i, row in enumerate(r["distribution"]):
        vals = [_fmt(x, row["fmt"]) for x in row["q"]] if row["q"] else [""] * 5
        mark = "^" if i == last else ""
        if row["is_component"]:
            a(f"| {mark}**{row['label']}** | " + " | ".join(f"**{x}**" for x in vals) + " |")
        else:
            a(f"| · {row['label']} | " + " | ".join(vals) + " |")

    # ── Eligibility sections ──
    a("\n## Stage 3: Eligibility\n")
    ne = e["scope"]
    ep = e["platform"]
    roll, sole = e["rollup"], e["sole"]

    # Inputs — the shared class-A scope, split by host.
    a("| Inputs | Count |")
    a("|---|--:|")
    a(f"| GitHub repos (Class A) | {ep['github']:,} |")
    a(f"| GitLab repos (Class A) | {ep['gitlab']:,} |")
    a(f"| ^**Total repos** | **{ne:,}** |")
    a("")

    # Outputs — how many scope repos pass each check.
    a("| Outputs | Count |")
    a("|---|--:|")
    a(f"| Open Source | {roll['oss']:,} |")
    a(f"| Active | {roll['active']:,} |")
    a(f"| Intent | {roll['intent']:,} |")
    a(f"| Nonprofit | {roll['nonprofit']:,} |")
    a(f"| ^**Eligibility checked** | **{ne:,}** |")
    a("")

    # Distribution — per check: sole blocker / fail (No) / pass (Yes) / total,
    # with the source/signal/reason breakdown beneath each. Sub-rows split
    # their own population: yes/no on the parent check, solo = repos where
    # that attribute alone decides the verdict (see eligibility_stats).
    subs = e["dist_subs"]
    a("| Distribution | Solo blocker | No | Yes | Count |")
    a("|---|--:|--:|--:|--:|")

    def _check(label: str, flag: str) -> None:
        yes = roll[flag]
        a(f"| ^**{label}** | {sole[flag]:,} | {ne - yes:,} | {yes:,} | {ne:,} |")

    def _subrow(label: str, d: dict) -> None:
        a(f"| · {label} | {d['solo']:,} | {d['no']:,} | {d['yes']:,} | {d['count']:,} |")

    for flag, label in (("oss", "Open Source"), ("active", "Active"),
                        ("intent", "Intent"), ("nonprofit", "Nonprofit")):
        _check(label, flag)
        for sub_label, d in subs[flag]:
            _subrow(sub_label, d)

    a(f"| ^**Eligible** |  | {ne - roll['eligible']:,} | "
      f"**{roll['eligible']:,}** | {ne:,} |")
    return "\n".join(out)


def pipeline_json(v: dict, r: dict, e: dict) -> dict:
    """The same numbers as the pipeline sheet, as a machine-readable payload.

    The dashboard (app.endowment.dev/model) reads this instead of the xlsx —
    the top-of-funnel counts (`tracked`, ~3.8M packages) come from multi-100MB
    git-LFS source files a Worker cannot open, so they must be pre-computed
    here. Regenerated on every build alongside preview.xlsx, so it never
    drifts from the sheet.
    """
    funnel = [
        {"stage": stage, "count": count, "denom": denom,
         "denom_label": denom_label, "comment": comment}
        for stage, count, denom, denom_label, comment in funnel_stats(v, r, e)
    ]
    return {"version": 1, "value": v, "risk": r,
            "eligibility": e, "funnel": funnel}


DEFAULT_JSON_PATH = ROOT / "data" / "preview" / "pipeline.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute the pipeline statistics.")
    ap.add_argument("--markdown", action="store_true",
                    help="emit the stats tables as markdown (the preview "
                         "workbook's pipeline sheet builds from this renderer)")
    ap.add_argument("--json", nargs="?", const=str(DEFAULT_JSON_PATH),
                    metavar="PATH", default=None,
                    help="write the pipeline numbers as JSON for the dashboard "
                         f"(default: {DEFAULT_JSON_PATH.relative_to(ROOT)})")
    args = ap.parse_args()

    v, r, e = value_stats(), risk_stats(), eligibility_stats()
    if args.markdown:
        print(markdown(v, r, e))
        return 0
    if args.json is not None:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(pipeline_json(v, r, e), indent=2) + "\n")
        console.print(f"wrote {out}")
        return 0
    dashboard(v, r, e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
