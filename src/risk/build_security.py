#!/usr/bin/env python3
"""Build data/risk/security.csv — security metrics per risk-scope repo.

Reads (long-format, sha-pinned where applicable):
    data/value/value.csv                     — A/B value-class set
    data/sources/github/git/commits-years.csv       — per (repo, year) last_sha
    data/sources/git/openssf.csv                    — long: scorecard `score` + 18
                                              individual checks per (repo, sha)
    data/sources/git/depsdev.csv                    — long: deps.dev-mirrored Scorecard
                                              `score` + checks per (repo, sha);
                                              fall-back when local row missing
    data/sources/git/semgrep.csv                    — long: semgrep findings per
                                              (repo, sha, rulepack-prefixed
                                              metric); locked to p_default
    data/sources/osv/cves.csv                       — per-CVE rows (repo, date, cve);
                                              5y count = distinct CVEs in
                                              2021..2025
    data/sources/ossfuzz/projects.csv               — projects enrolled in OSS-Fuzz
    data/sources/depsdev/repos.csv                  — non-sha enrichment
                                              (bestpractices_badge_id)

Writes:
    data/risk/security.csv  with columns:
        repo, repo_id,
        openssf_score,                      ([2025 EOY], 0..10) — local first,
                                            falls back to deps.dev mirror
        openssf_score_source,               "openssf_local" | "depsdev" | ""
        cve_count_5y,                       ([2021–2025], distinct CVE ids)
        ossfuzz_enrolled,                   ([most recent], "True"/"False")
        sast_findings_total, sast_findings_total_p,       ([2025 EOY] semgrep p/default + pctl)
        sast_findings_error, sast_findings_error_p,       ([2025 EOY] high-severity only + pctl)
        sast_findings_security, sast_findings_security_p, ([2025 EOY] security-category only + pctl)
        bestpractices_badge_id,             ([2026], "passing"/"silver"/"gold"/
                                            "in_progress"/"" if not enrolled)
        openssf_score_p,                    (0–100 risk percentile of
                                            openssf_score, lower-is-worse — a
                                            lower Scorecard score ranks higher-risk)
        cve_score,                          (0–100 neutral-anchored CVE risk
                                            score: 0 known CVEs → 50, ≥1 CVE
                                            ranked into (50, 100], worst → 100)
        score,                              (geometric mean of openssf_score_p
                                            and cve_score; "" if either
                                            openssf_score or cve_count_5y missing)
        fetched_at                          (checked_at of openssf row used)

    The sast_*_p columns are informational and NOT part of `score`.

Latest-sha picker
-----------------
For each long file, we walk per-repo year priority 2025→2024→…→2021 from
`commits-years.csv` and pick the first sha that has any rows in that file.
This keeps the build aligned with the same "snapshot year" convention used
by build_complexity. If commits-years has no usable year for a repo, we
fall back to any sha present in the long file for that repo (deterministic
lexicographic pick).

Security score
--------------
`score` is the geometric mean of two direction-aware risk axes: `cve_score`
(0 known CVEs → 50, ≥1 CVE ranked into (50, 100]) and `openssf_score_p`
(lower Scorecard score → higher risk). It is populated only when both
`openssf_score` and `cve_count_5y` are present.

~78% of risk-scope repos have zero CVEs and all share `cve_score = 50` — a
neutral baseline ("none known" ≠ "proven secure"), not the worst-pinned CDF's
78. For those repos `score` tracks the OpenSSF axis, with the CVE axis only
re-ranking the minority that carry CVEs, above the neutral 50.

Usage:
    uv run python -m src.risk.build_security
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.params import YEARS
from src.common.percentiles import add_percentiles
from src.common.repos import canonical_repo_map, load_risk_repos
from src.common.stats import floor_anchored_risk
from src.common.tables import load_column_by_repo
from src.sources.git.long_format import read as read_long

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
GIT_LONG_DIR = DATA_DIR / "sources" / "git"
COMMITS_YEARS_FILE = DATA_DIR / "sources" / "github" / "git" / "commits-years.csv"
OPENSSF_FILE = GIT_LONG_DIR / "openssf.csv"
DEPSDEV_LONG_FILE = GIT_LONG_DIR / "depsdev.csv"
SEMGREP_FILE = GIT_LONG_DIR / "semgrep.csv"
OSV_FILE = DATA_DIR / "sources" / "osv" / "cves.csv"
OSSFUZZ_FILE = DATA_DIR / "sources" / "ossfuzz" / "projects.csv"
DEPSDEV_REPOS_FILE = DATA_DIR / "sources" / "depsdev" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "security.csv"

# Semgrep rule pack to surface in the build. Same lock as before — risk.py
# expects p_default values across runs.
SEMGREP_PREFIX = "p_default."

FIELDS = [
    "repo", "repo_id",
    "openssf_score", "openssf_score_source",
    "cve_count_5y", "ossfuzz_enrolled",
    "sast_findings_total", "sast_findings_total_p",
    "sast_findings_error", "sast_findings_error_p",
    "sast_findings_security", "sast_findings_security_p",
    "bestpractices_badge_id",
    "openssf_score_p", "cve_score",
    "score",
    "fetched_at",
]


def _per_year_shas(commits_years_file: Path) -> dict[str, list[str]]:
    """Return {repo: [sha, …]} newest-first across the settings `years` window.

    Only includes years with non-empty `last_sha` AND `commits > 0`.
    Repos with no usable year get no key.
    """
    by_repo: dict[str, dict[int, str]] = {}
    if not commits_years_file.exists():
        return {}
    with open(commits_years_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            last_sha = (row.get("last_sha") or "").strip()
            if not last_sha:
                continue
            try:
                year = int((row.get("year") or "").strip())
                commits = int((row.get("commits") or "0").strip())
            except ValueError:
                continue
            if commits <= 0:
                continue
            if min(YEARS) <= year <= max(YEARS):
                by_repo.setdefault(slug, {})[year] = last_sha

    out: dict[str, list[str]] = {}
    for repo, year_map in by_repo.items():
        # Walk newest → oldest; keep order, skip duplicates.
        ordered: list[str] = []
        seen: set[str] = set()
        for y in sorted(YEARS, reverse=True):
            sha = year_map.get(y)
            if sha and sha not in seen:
                ordered.append(sha)
                seen.add(sha)
        if ordered:
            out[repo] = ordered
    return out


def _index_long_by_repo_sha(
    rows: dict[tuple[str, str, str], dict[str, str]],
    metrics: set[str] | None = None,
    metric_prefix: str | None = None,
) -> dict[tuple[str, str], dict[str, dict[str, str]]]:
    """Index {(repo, sha): {metric: row}} from long-format rows.

    `metrics` and `metric_prefix` are optional filters (either or both).
    Stores the full row (so callers can read `value` and `checked_at`).
    """
    out: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
    for (repo, sha, metric), row in rows.items():
        if metrics is not None and metric not in metrics:
            continue
        if metric_prefix is not None and not metric.startswith(metric_prefix):
            continue
        out.setdefault((repo, sha), {})[metric] = row
    return out


def _pick_latest(
    repo: str,
    sha_priority: list[str],
    sha_index: dict[tuple[str, str], dict[str, dict[str, str]]],
    fallback_shas_per_repo: dict[str, list[str]],
) -> tuple[str, dict[str, dict[str, str]]] | None:
    """Pick (sha, metric_rows) for `repo` using year priority then fallback.

    1. Try each sha in `sha_priority` (newest year first); first hit wins.
    2. If none match, fall back to any sha present for the repo in the
       index (lexicographically smallest — deterministic).
    Returns None if nothing matches.
    """
    for sha in sha_priority:
        rows = sha_index.get((repo, sha))
        if rows:
            return sha, rows
    for sha in fallback_shas_per_repo.get(repo, []):
        rows = sha_index.get((repo, sha))
        if rows:
            return sha, rows
    return None


def _shas_per_repo(
    sha_index: dict[tuple[str, str], dict[str, dict[str, str]]],
) -> dict[str, list[str]]:
    """Group shas present in the index by repo, sorted lex (deterministic)."""
    by_repo: dict[str, list[str]] = {}
    for (repo, sha) in sha_index.keys():
        by_repo.setdefault(repo, []).append(sha)
    for repo in by_repo:
        by_repo[repo].sort()
    return by_repo


def _load_ossfuzz() -> set[str]:
    """Return the set of GitHub repos enrolled in OSS-Fuzz (canonical slugs)."""
    out: set[str] = set()
    if not OSSFUZZ_FILE.exists():
        return out
    canon = canonical_repo_map()
    with open(OSSFUZZ_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("github_repo") or "").strip().lower()
            if slug:
                out.add(canon.get(slug, slug))
    return out


def _load_cve_counts_5y() -> dict[str, int]:
    """Count distinct CVE ids per repo within the settings `years` window.

    Each row in osv/cves.csv is one (repo, cve, package-source) tuple.
    Multiple package mappings can produce duplicate (repo, cve) pairs —
    we dedupe on the CVE id within a repo. Date filter uses the `date`
    column's first 4 chars (YYYY).
    """
    counts: dict[str, set[str]] = {}
    if not OSV_FILE.exists():
        return {}
    with open(OSV_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            cve = (row.get("cve") or "").strip()
            date = (row.get("date") or "").strip()
            if not slug or not cve or len(date) < 4:
                continue
            year = date[:4]
            if year < str(min(YEARS)) or year > str(max(YEARS)):
                continue
            counts.setdefault(slug, set()).add(cve)
    return {slug: len(cves) for slug, cves in counts.items()}


def _load_osv_queried() -> set[str]:
    """Repos we attempted to query OSV for (sidecar to cves.csv).

    Used to distinguish "0 CVEs because we asked and got none" from
    "missing because we never queried". For now we just count rows in
    cves.csv directly — a repo absent from cves.csv but present in
    queried.csv is a confirmed zero.
    """
    out: set[str] = set()
    queried_file = DATA_DIR / "sources" / "osv" / "queried.csv"
    if not queried_file.exists():
        return out
    with open(queried_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out.add(slug)
    return out


def build() -> list[dict]:
    eligible = load_risk_repos()

    per_year = _per_year_shas(COMMITS_YEARS_FILE)

    # Index each long file by (repo, sha) → {metric: row}.
    openssf_idx = _index_long_by_repo_sha(read_long(OPENSSF_FILE))
    depsdev_idx = _index_long_by_repo_sha(read_long(DEPSDEV_LONG_FILE))
    semgrep_idx = _index_long_by_repo_sha(
        read_long(SEMGREP_FILE), metric_prefix=SEMGREP_PREFIX,
    )

    openssf_shas = _shas_per_repo(openssf_idx)
    depsdev_shas = _shas_per_repo(depsdev_idx)
    semgrep_shas = _shas_per_repo(semgrep_idx)

    fuzz = _load_ossfuzz()
    cve_counts = _load_cve_counts_5y()
    queried = _load_osv_queried()
    badges = load_column_by_repo(DEPSDEV_REPOS_FILE, "bestpractices_badge_id")

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        priority = per_year.get(repo, [])

        # OpenSSF Scorecard: local (openssf.csv) → deps.dev mirror fallback.
        ossf_score = ""
        ossf_source = ""
        ossf_checked_at = ""
        local = _pick_latest(repo, priority, openssf_idx, openssf_shas)
        if local is not None:
            _sha, metrics = local
            score_row = metrics.get("score")
            if score_row and score_row.get("value", "").strip():
                ossf_score = score_row["value"].strip()
                ossf_source = "openssf_local"
                ossf_checked_at = (score_row.get("checked_at") or "").strip()
        if not ossf_score:
            mirror = _pick_latest(repo, priority, depsdev_idx, depsdev_shas)
            if mirror is not None:
                _sha, metrics = mirror
                score_row = metrics.get("score")
                if score_row and score_row.get("value", "").strip():
                    ossf_score = score_row["value"].strip()
                    ossf_source = "depsdev"
                    ossf_checked_at = (
                        score_row.get("checked_at") or ""
                    ).strip()

        # Semgrep p/default findings.
        sast_total = sast_error = sast_security = ""
        sg = _pick_latest(repo, priority, semgrep_idx, semgrep_shas)
        if sg is not None:
            _sha, metrics = sg
            t = metrics.get(SEMGREP_PREFIX + "findings_total")
            e = metrics.get(SEMGREP_PREFIX + "findings_error")
            s = metrics.get(SEMGREP_PREFIX + "findings_security")
            if t and t.get("value", "").strip():
                sast_total = t["value"].strip()
            if e and e.get("value", "").strip():
                sast_error = e["value"].strip()
            if s and s.get("value", "").strip():
                sast_security = s["value"].strip()

        # CVE count: count from cves.csv if present; else 0 if we queried;
        # else "" (unknown — never queried).
        if repo in cve_counts:
            cve_5y = str(cve_counts[repo])
        elif repo in queried:
            cve_5y = "0"
        else:
            cve_5y = ""

        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "openssf_score": ossf_score,
            "openssf_score_source": ossf_source,
            "cve_count_5y": cve_5y,
            "ossfuzz_enrolled": "True" if repo in fuzz else "False",
            "sast_findings_total": sast_total,
            "sast_findings_error": sast_error,
            "sast_findings_security": sast_security,
            "bestpractices_badge_id": badges.get(repo, ""),
            "fetched_at": ossf_checked_at,
        })

    # CVE risk score — neutral-anchored. 0 known CVEs → 50 (not the worst-pinned
    # CDF's 78); "none known" is treated as neutral, not proven secure. Repos
    # with ≥1 CVE rank among themselves into (50, 100], worst → 100.
    def _num(s: str) -> float | None:
        s = (s or "").strip()
        try:
            return float(s) if s else None
        except ValueError:
            return None

    cve_scores = floor_anchored_risk(
        [_num(r.get("cve_count_5y", "")) for r in rows], floor=0.0, anchor=50.0
    )
    for r, s in zip(rows, cve_scores):
        r["cve_score"] = "" if s is None else round(s, 2)

    # Second pass — openssf risk percentile + the informational sast pctls, then
    # the composite `score` = geom-mean(openssf_score_p, cve_score).
    add_percentiles(
        rows,
        pctl_specs=[
            ("openssf_score", False),
            ("sast_findings_total", True), ("sast_findings_error", True),
            ("sast_findings_security", True),
        ],
        composite_cols=["openssf_score_p", "cve_score"],
        dim_col="score",
    )

    return rows


def main() -> None:
    console.print("[bold]Building security.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Security coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in ("openssf_score", "cve_count_5y", "ossfuzz_enrolled",
                "sast_findings_total", "sast_findings_error",
                "sast_findings_security", "bestpractices_badge_id",
                "openssf_score_p", "cve_score", "score"):
        n = sum(1 for r in rows if r.get(col) not in ("", None))
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")

    src_counts = Counter(
        r.get("openssf_score_source") or "missing" for r in rows
    )
    console.print(table)
    console.print(
        f"[dim]openssf_score sources: "
        f"local={src_counts.get('openssf_local', 0):,} "
        f"depsdev={src_counts.get('depsdev', 0):,} "
        f"missing={src_counts.get('missing', 0):,}[/dim]"
    )

    enrolled = sum(1 for r in rows if r["ossfuzz_enrolled"] == "True")
    console.print(f"\n[dim]OSS-Fuzz enrolled: {enrolled:,} / {total:,}[/dim]")

    console.print(f"[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
