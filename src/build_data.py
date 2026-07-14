#!/usr/bin/env python3
"""Build data/preview/data.csv — the evidence behind every score in repos.csv.

Same repos, same order as `src.build_results` (score desc), one row each — but
where repos.csv shows the *verdicts* (value_score, risk_score, eligible), this
table shows the **measurements those verdicts were computed from**: LOC, active
contributors, bus factor, CVE counts, download volume, PageRank mass, licence,
funding channels.

Inclusion rule — a column earns its place only if a builder actually READS it
to produce a score or an eligibility flag, plus the components/verdicts it
produces. Everything a fetcher merely collected along the way (fetch dates,
`ossfuzz_enrolled`, `gh_sponsorships`, `issue_close_ratio`, the lifetime
concentration columns, the per-ecosystem `class_*` gates, `value_comps` /
`risk_comps` / `complete`) is left out: it did not move a number.

Two consequences worth knowing when reading a row:

  - Percentile (`*_p`) columns are omitted. Complexity, security and workload
    score off percentile ranks of the raw columns shown here, so a cell's
    contribution is relative to the other 945 repos, not absolute.
  - `intent` propagates at the OWNER level (build_funding): one repo of an
    owner declaring a channel flips intent=True for all its siblings. A row can
    therefore read intent=True with every funding signal on it blank.

Column names are kept EXACTLY as they appear in the source CSV (`loc_eoy`,
`bf_commits_git_5y`, `oc_slug`), so any cell can be traced back to the file it
came from. The exceptions are the columns this script derives itself, because
the pipeline holds them only in memory (unify strips them before writing
value.csv):

    downloads_<registry>   sum of avg_downloads over the repo's packages in
                           that registry (cpp splits into its two real
                           registries, Debian popcon + Homebrew installs)
    pagerank_<eco>         sum of the repo's packages' PageRank in that
                           ecosystem — the raw dependency mass behind
                           top_eco_pct and pr_score
    crit_*                 the OpenSSF criticality tool's own input signals
                           (data/sources/openssf/criticality.csv), prefixed so
                           they can't be confused with the git-derived risk
                           columns (crit_contributors is GitHub's contributor
                           list; active_contributors_git_5y is the commit log's)

Usage:
    uv run python -m src.build_data
"""

import csv
from pathlib import Path

from rich.console import Console

from src.build_results import build as build_repo_rows
from src.common.tables import load_rows_by_id

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VALUE_FILE = DATA_DIR / "value" / "value.csv"
CONCENTRATION_FILE = DATA_DIR / "risk" / "concentration.csv"
COMPLEXITY_FILE = DATA_DIR / "risk" / "complexity.csv"
SECURITY_FILE = DATA_DIR / "risk" / "security.csv"
WORKLOAD_FILE = DATA_DIR / "risk" / "workload.csv"
LICENSES_FILE = DATA_DIR / "eligibility" / "licenses.csv"
ACTIVE_FILE = DATA_DIR / "eligibility" / "active.csv"
FUNDING_FILE = DATA_DIR / "eligibility" / "funding.csv"
CRITICALITY_FILE = DATA_DIR / "sources" / "openssf" / "criticality.csv"
GITHUB_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
GITLAB_REPOS_FILE = DATA_DIR / "sources" / "gitlab" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "preview" / "data.csv"

# Per-ecosystem package tables — one row per package, `repo_id`-stamped by the
# value stage, so a repo's packages sum straight onto its row.
ECOSYSTEMS = ["npm", "pypi", "crates", "cpp"]
RESULTS_FILE = {eco: DATA_DIR / "sources" / eco / "results.csv" for eco in ECOSYSTEMS}
# The download column each registry's results.csv actually carries. cpp is a
# merged ecosystem (one PageRank graph) but two real registries with two
# incomparable units — Debian popcon installs and Homebrew installs — so they
# stay separate columns rather than being summed into a fake total.
DOWNLOAD_COLS = {
    "npm": {"downloads_npm": "avg_downloads"},
    "pypi": {"downloads_pypi": "avg_downloads"},
    "crates": {"downloads_crates": "avg_downloads"},
    "cpp": {"downloads_debian": "debian_avg_downloads",
            "downloads_homebrew": "homebrew_avg_downloads"},
}

# OpenSSF criticality's own inputs → the raw signals behind openssf_crit, the
# heaviest value component (weight 0.6). `star_count` is dropped: it is the same
# measurement as the `stars` column, fetched by a different source.
CRIT_SIGNALS = {
    "crit_contributors": "contributor_count",
    "crit_orgs": "org_count",
    "crit_commit_freq": "commit_frequency",
    "crit_releases": "recent_release_count",
    "crit_issues_updated": "updated_issues_count",
    "crit_issues_closed": "closed_issues_count",
    "crit_issue_comment_freq": "issue_comment_frequency",
    "crit_mentions": "github_mention_count",
    "crit_created_since": "created_since",
    "crit_updated_since": "updated_since",
}

FIELDS = (
    # identity
    ["repo", "platform", "stars", "forks", "ecosystems", "packages",
     "ecosystem", "top_eco_pkg"]
    # value — raw: registry demand, dependency mass, criticality's own signals
    + list(DOWNLOAD_COLS["npm"]) + list(DOWNLOAD_COLS["pypi"])
    + list(DOWNLOAD_COLS["crates"]) + list(DOWNLOAD_COLS["cpp"])
    + [f"pagerank_{eco}" for eco in ECOSYSTEMS]
    + list(CRIT_SIGNALS)
    # value — components → value_score
    + ["top_eco_pct", "pr_score", "openssf_crit", "eco_crit", "value_score"]
    # risk — raw inputs, in the order the dimensions consume them
    + ["active_contributors_git_5y", "dormant", "bf_commits_git_5y",
       "hhi_commits_git_5y", "loc_eoy", "cyclomatic_max", "openssf_score",
       "cve_count_5y", "issues_opened_5y", "issues_closed_5y",
       "net_new_issues_5y"]
    # risk — components → risk_score
    + ["concentration", "complexity", "security", "workload", "risk_score"]
    # eligibility — raw signals behind each boolean
    + ["license", "license_source", "eol", "archived",
       "gh_sponsors_enabled", "has_funding_yml", "has_funding_json",
       "has_funding_links", "has_npm_funding", "has_pypi_funding",
       "bf_maintainer_fundable", "paypal", "oc_slug",
       "host", "host_type", "owner", "owner_type"]
    # eligibility — verdicts
    + ["oss", "intent", "nonprofit", "active", "eligible"]
    # preview results
    + ["score", "priority", "repo_id"]
)


def _sum_by_repo(path: Path, columns: dict[str, str]) -> dict[str, dict[str, float]]:
    """Sum the named package-level columns per repo_id: {repo_id: {out: total}}.

    Rows with a blank `repo_id` (a package whose repo never resolved) are
    skipped — they belong to no row of this table. A repo with packages but no
    parseable value in a column totals 0.0, which is a real measured zero.
    """
    totals: dict[str, dict[str, float]] = {}
    if not path.exists():
        return totals
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            if not rid:
                continue
            bucket = totals.setdefault(rid, {out: 0.0 for out in columns})
            for out, src in columns.items():
                try:
                    bucket[out] += float(row.get(src) or 0)
                except ValueError:
                    pass
    return totals


def _package_totals() -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """(downloads, pagerank) per repo_id, summed over each ecosystem's packages."""
    downloads: dict[str, dict[str, float]] = {}
    pagerank: dict[str, dict[str, float]] = {}
    for eco in ECOSYSTEMS:
        cols = {**DOWNLOAD_COLS[eco], f"pagerank_{eco}": "pagerank"}
        for rid, sums in _sum_by_repo(RESULTS_FILE[eco], cols).items():
            for out, total in sums.items():
                target = pagerank if out.startswith("pagerank_") else downloads
                target.setdefault(rid, {})[out] = total
    return downloads, pagerank


def build() -> list[dict]:
    """One row per repo of repos.csv (same order), joined to every stage input."""
    value = load_rows_by_id(VALUE_FILE)
    concentration = load_rows_by_id(CONCENTRATION_FILE)
    complexity = load_rows_by_id(COMPLEXITY_FILE)
    security = load_rows_by_id(SECURITY_FILE)
    workload = load_rows_by_id(WORKLOAD_FILE)
    licenses = load_rows_by_id(LICENSES_FILE)
    active = load_rows_by_id(ACTIVE_FILE)
    funding = load_rows_by_id(FUNDING_FILE)
    criticality = load_rows_by_id(CRITICALITY_FILE)
    github = load_rows_by_id(GITHUB_REPOS_FILE)
    gitlab = load_rows_by_id(GITLAB_REPOS_FILE)
    downloads, pagerank = _package_totals()

    rows: list[dict] = []
    for result in build_repo_rows():
        rid = result["repo_id"]
        v, cc = value.get(rid, {}), concentration.get(rid, {})
        cx, sec, wl = complexity.get(rid, {}), security.get(rid, {}), workload.get(rid, {})
        lic, act, fund = licenses.get(rid, {}), active.get(rid, {}), funding.get(rid, {})
        # A criticality row that didn't run cleanly carries no usable signals —
        # the value stage ignores it, and so does this table (blank, not zero).
        crit = criticality.get(rid, {})
        if (crit.get("status") or "").strip().lower() != "ok":
            crit = {}
        # Stars/forks: GitHub's repos.csv, else the GitLab project API.
        host = github.get(rid) or gitlab.get(rid, {})
        dl, pr = downloads.get(rid, {}), pagerank.get(rid, {})

        row = {
            "repo": result["repo"],
            "platform": result["platform"],
            "stars": (host.get("stars") or "").strip(),
            "forks": (host.get("forks") or "").strip(),
            "ecosystems": (v.get("ecosystems") or "").strip(),
            "packages": (v.get("packages") or "").strip(),
            "ecosystem": result["ecosystem"],
            "top_eco_pkg": result["top_eco_pkg"],
            # Package sums: blank (not 0) where the repo has no package in that
            # registry at all — a repo absent from npm did not score 0 downloads.
            **{col: (f"{dl[col]:.0f}" if col in dl else "")
               for eco in ECOSYSTEMS for col in DOWNLOAD_COLS[eco]},
            **{f"pagerank_{eco}": (f"{pr[f'pagerank_{eco}']:.6f}"
                                   if f"pagerank_{eco}" in pr else "")
               for eco in ECOSYSTEMS},
            **{out: (crit.get(src) or "").strip() for out, src in CRIT_SIGNALS.items()},
            **{col: result[col] for col in
               ("top_eco_pct", "pr_score", "openssf_crit", "eco_crit", "value_score")},
            "active_contributors_git_5y": (cc.get("active_contributors_git_5y") or "").strip(),
            "dormant": (wl.get("dormant") or "").strip(),
            "bf_commits_git_5y": (cc.get("bf_commits_git_5y") or "").strip(),
            "hhi_commits_git_5y": (cc.get("hhi_commits_git_5y") or "").strip(),
            "loc_eoy": (cx.get("loc_eoy") or "").strip(),
            "cyclomatic_max": (cx.get("cyclomatic_max") or "").strip(),
            "openssf_score": (sec.get("openssf_score") or "").strip(),
            "cve_count_5y": (sec.get("cve_count_5y") or "").strip(),
            "issues_opened_5y": (wl.get("issues_opened_5y") or "").strip(),
            "issues_closed_5y": (wl.get("issues_closed_5y") or "").strip(),
            "net_new_issues_5y": (wl.get("net_new_issues_5y") or "").strip(),
            **{col: result[col] for col in
               ("concentration", "complexity", "security", "workload", "risk_score")},
            "license": (lic.get("license") or "").strip(),
            "license_source": (lic.get("license_source") or "").strip(),
            "eol": (act.get("eol") or "").strip(),
            "archived": (act.get("archived") or "").strip(),
            **{col: (fund.get(col) or "").strip() for col in
               ("gh_sponsors_enabled", "has_funding_yml", "has_funding_json",
                "has_funding_links", "has_npm_funding", "has_pypi_funding",
                "bf_maintainer_fundable", "paypal", "oc_slug",
                "host", "host_type", "owner", "owner_type")},
            **{col: result[col] for col in
               ("oss", "intent", "nonprofit", "active", "eligible",
                "score", "priority", "repo_id")},
        }
        rows.append(row)
    return rows


def main() -> None:
    rows = build()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    filled = {col: sum(1 for r in rows if r[col] != "") for col in FIELDS}
    thin = [c for c, n in filled.items() if n < len(rows) * 0.5]
    console.print(
        f"[dim]{len(rows):,} repos × {len(FIELDS)} data points → {OUTPUT_FILE}[/dim]"
    )
    if thin:
        console.print(f"[dim]  under half-filled: {', '.join(thin)}[/dim]")


if __name__ == "__main__":
    main()
