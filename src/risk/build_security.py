#!/usr/bin/env python3
"""Build data/risk/security.csv — security metrics per risk-scope repo.

Reads (long-format, sha-pinned where applicable):
    data/value/value.csv                     — A/B value-class set
    data/sources/git/commits-years.csv       — per (repo, year) last_sha
    data/sources/git/openssf.csv                    — long: scorecard `score` + 18
                                              individual checks per (repo, sha)
    data/sources/git/depsdev.csv                    — long: deps.dev-mirrored Scorecard
                                              `score` + checks per (repo, sha);
                                              fall-back when local row missing
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
        bestpractices_badge_id,             ([2026], "passing"/"silver"/"gold"/
                                            "in_progress"/"" if not enrolled)
        openssf_score_p,                    (0–100 risk percentile of
                                            openssf_score, lower-is-worse — a
                                            lower Scorecard score ranks higher-risk)
        cve_score,                          (0–100 neutral-anchored CVE risk
                                            score: 0 known CVEs → 50, ≥1 CVE
                                            ranked into (50, 100], worst → 100)
        score,                              (max of openssf_score_p and
                                            cve_score over whichever axes are
                                            present; "" only when BOTH are)
        fetched_at                          (checked_at of openssf row used)

Latest-sha picker
-----------------
For each long file, we walk per-repo year priority 2025→2024→…→2021 from
`commits-years.csv` and pick the first sha that has any rows in that file.
If commits-years has no usable year for a repo, we fall back to any sha
present in the long file for that repo (deterministic lexicographic pick).

This is NOT the same picker build_complexity uses. Both take years with a
non-empty `last_sha` and `commits > 0`, then differ:

  - here, `_per_year_shas` clamps to the settings `years` window, so a
    pre-window snapshot is unreachable;
  - build_complexity keeps EVERY real year, letting a dormant repo fall back
    to a pre-window snapshot, and additionally requires scc `loc > 0`;
  - the no-usable-year fallback differs too: lexicographic sha here, the
    empty-tree sha there.

A Scorecard scan is a point-in-time audit, so an in-window snapshot is the
only meaningful one — an old scan does not describe today's repo. LOC is
cumulative, so complexity gains from walking further back.

Security score
--------------
`score` is the max ("worst-of") of two direction-aware risk axes: `cve_score`
(0 known CVEs → 50, ≥1 CVE ranked into (50, 100]) and `openssf_score_p`
(lower Scorecard score → higher risk). It is composed with `max_composite_any`,
so it is the max over the axes PRESENT: a repo with a CVE count but no
Scorecard row still scores on the CVE axis alone, and `score` is blank only
when both axes are missing. Max rather than geometric mean means the two axes
do not compound: either a bad Scorecard alone or a real CVE alone is enough to
flag the repo, and neither dilutes the other.

Most risk-scope repos have zero CVEs and so share `cve_score = 50` — a neutral
baseline ("none known" ≠ "proven secure"), not a worst-pinned CDF rank. For
those repos `score` = openssf_score_p whenever that axis clears 50 (it does for
most), so the score tracks the OpenSSF axis; the CVE axis takes over only for
the minority carrying CVEs whose `cve_score` exceeds the openssf axis — a repo
with real CVEs is never masked by otherwise-good hygiene. (Coverage counts live
on the preview pipeline sheet, not here.)

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
from src.common.repos import canonical_repo_map, load_top_repos
from src.common.stats import floor_anchored_risk, max_composite_any
from src.common.tables import load_column_by_id
from src.sources.git.long_format import read as read_long

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
GIT_LONG_DIR = DATA_DIR / "sources" / "git"
COMMITS_YEARS_FILE = DATA_DIR / "sources" / "git" / "commits-years.csv"
OPENSSF_FILE = GIT_LONG_DIR / "openssf.csv"
DEPSDEV_LONG_FILE = GIT_LONG_DIR / "depsdev.csv"
OSV_FILE = DATA_DIR / "sources" / "osv" / "cves.csv"
OSSFUZZ_FILE = DATA_DIR / "sources" / "ossfuzz" / "projects.csv"
DEPSDEV_REPOS_FILE = DATA_DIR / "sources" / "depsdev" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "security.csv"

FIELDS = [
    "repo", "repo_id",
    "openssf_score", "openssf_score_source",
    "cve_count_5y", "ossfuzz_enrolled",
    "bestpractices_badge_id",
    "openssf_score_p", "cve_score",
    "score",
    "fetched_at",
]


def _per_year_shas(commits_years_file: Path) -> dict[str, list[str]]:
    """Return {repo_id: [sha, …]} newest-first across the settings `years` window.

    Keyed by GitHub's stable numeric `repo_id`, not the repo name, so a
    renamed/moved repo still matches the sha priority collected under its old
    name. Only includes years with non-empty `last_sha` AND `commits > 0`.
    Repos with no usable year (or no repo_id) get no key.
    """
    by_repo: dict[str, dict[int, str]] = {}
    if not commits_years_file.exists():
        return {}
    with open(commits_years_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            if not rid:
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
                by_repo.setdefault(rid, {})[year] = last_sha

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
    """Index {(repo_id, sha): {metric: row}} from long-format rows.

    Keyed by the stable `repo_id` each long row carries — not the repo name —
    so metrics collected under a repo's old name still join to its current
    entry after a rename/move. Rows with a blank `repo_id` are skipped.
    `metrics` and `metric_prefix` are optional filters (either or both).
    Stores the full row (so callers can read `value` and `checked_at`).
    """
    out: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
    for (_repo, sha, metric), row in rows.items():
        if metrics is not None and metric not in metrics:
            continue
        if metric_prefix is not None and not metric.startswith(metric_prefix):
            continue
        rid = (row.get("repo_id") or "").strip()
        if not rid:
            continue
        out.setdefault((rid, sha), {})[metric] = row
    return out


def _pick_latest(
    repo_id: str,
    sha_priority: list[str],
    sha_index: dict[tuple[str, str], dict[str, dict[str, str]]],
    fallback_shas_per_repo: dict[str, list[str]],
) -> tuple[str, dict[str, dict[str, str]]] | None:
    """Pick (sha, metric_rows) for `repo_id` using year priority then fallback.

    `sha_index` and `fallback_shas_per_repo` are both keyed by the stable
    `repo_id`, so the lookup survives a repo rename.

    1. Try each sha in `sha_priority` (newest year first); first hit wins.
    2. If none match, fall back to any sha present for the repo_id in the
       index (lexicographically smallest — deterministic).
    Returns None if nothing matches.
    """
    for sha in sha_priority:
        rows = sha_index.get((repo_id, sha))
        if rows:
            return sha, rows
    for sha in fallback_shas_per_repo.get(repo_id, []):
        rows = sha_index.get((repo_id, sha))
        if rows:
            return sha, rows
    return None


def _shas_per_repo(
    sha_index: dict[tuple[str, str], dict[str, dict[str, str]]],
) -> dict[str, list[str]]:
    """Group shas present in the index by repo_id, sorted lex (deterministic)."""
    by_repo: dict[str, list[str]] = {}
    for (repo_id, sha) in sha_index.keys():
        by_repo.setdefault(repo_id, []).append(sha)
    for repo_id in by_repo:
        by_repo[repo_id].sort()
    return by_repo


def _load_ossfuzz() -> tuple[set[str], set[str]]:
    """Return OSS-Fuzz enrollment as (repo_ids, canonical slugs).

    Rows in ossfuzz/projects.csv that carry a stable `repo_id` join by id —
    rename-proof, like every other GitHub-identity join in this builder.
    Rows with a blank `repo_id` (out-of-scope slugs the fetcher could not
    resolve) fall back to canonical-slug matching, as before.
    """
    ids: set[str] = set()
    slugs: set[str] = set()
    if not OSSFUZZ_FILE.exists():
        return ids, slugs
    canon = canonical_repo_map()
    with open(OSSFUZZ_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("github_repo") or "").strip().lower()
            if not slug:
                continue
            rid = (row.get("repo_id") or "").strip()
            if rid:
                ids.add(rid)
            else:
                slugs.add(canon.get(slug, slug))
    return ids, slugs


def _load_cve_counts_5y() -> dict[str, int]:
    """Count distinct CVE ids per repo_id within the settings `years` window.

    Keyed by GitHub's stable `repo_id` so a renamed repo's CVEs still join.
    Each row in osv/cves.csv is one (repo, cve, package-source) tuple.
    Multiple package mappings can produce duplicate (repo_id, cve) pairs —
    we dedupe on the CVE id within a repo. Date filter uses the `date`
    column's first 4 chars (YYYY).
    """
    counts: dict[str, set[str]] = {}
    if not OSV_FILE.exists():
        return {}
    with open(OSV_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            cve = (row.get("cve") or "").strip()
            date = (row.get("date") or "").strip()
            if not rid or not cve or len(date) < 4:
                continue
            year = date[:4]
            if year < str(min(YEARS)) or year > str(max(YEARS)):
                continue
            counts.setdefault(rid, set()).add(cve)
    return {rid: len(cves) for rid, cves in counts.items()}


def _load_osv_queried() -> set[str]:
    """repo_ids we attempted to query OSV for (sidecar to cves.csv).

    Keyed by the stable `repo_id` (rename-proof), matching cve counts.
    Used to distinguish "0 CVEs because we asked and got none" from
    "missing because we never queried": a repo absent from cves.csv but
    present in queried.csv is a confirmed zero.
    """
    out: set[str] = set()
    queried_file = DATA_DIR / "sources" / "osv" / "queried.csv"
    if not queried_file.exists():
        return out
    with open(queried_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            if rid:
                out.add(rid)
    return out


def build() -> list[dict]:
    eligible = load_top_repos()

    per_year = _per_year_shas(COMMITS_YEARS_FILE)

    # Index each long file by (repo_id, sha) → {metric: row}.
    openssf_idx = _index_long_by_repo_sha(read_long(OPENSSF_FILE))
    depsdev_idx = _index_long_by_repo_sha(read_long(DEPSDEV_LONG_FILE))

    openssf_shas = _shas_per_repo(openssf_idx)
    depsdev_shas = _shas_per_repo(depsdev_idx)

    fuzz_ids, fuzz_slugs = _load_ossfuzz()
    cve_counts = _load_cve_counts_5y()
    queried = _load_osv_queried()
    badges = load_column_by_id(DEPSDEV_REPOS_FILE, "bestpractices_badge_id")

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        # Every GitHub-identity join keys on the stable repo_id (rename-proof);
        # OSS-Fuzz rows the fetcher could not resolve to an id fall back to
        # canonical-slug matching (see _load_ossfuzz).
        rid = str(entry.repo_id)
        priority = per_year.get(rid, [])

        # OpenSSF Scorecard: local (openssf.csv) → deps.dev mirror fallback.
        ossf_score = ""
        ossf_source = ""
        ossf_checked_at = ""
        local = _pick_latest(rid, priority, openssf_idx, openssf_shas)
        if local is not None:
            _sha, metrics = local
            score_row = metrics.get("score")
            if score_row and score_row.get("value", "").strip():
                ossf_score = score_row["value"].strip()
                ossf_source = "openssf_local"
                ossf_checked_at = (score_row.get("checked_at") or "").strip()
        if not ossf_score:
            mirror = _pick_latest(rid, priority, depsdev_idx, depsdev_shas)
            if mirror is not None:
                _sha, metrics = mirror
                score_row = metrics.get("score")
                if score_row and score_row.get("value", "").strip():
                    ossf_score = score_row["value"].strip()
                    ossf_source = "depsdev"
                    ossf_checked_at = (
                        score_row.get("checked_at") or ""
                    ).strip()

        # CVE count (by repo_id): count from cves.csv if present; else 0 if we
        # queried; else "" (unknown — never queried).
        if rid in cve_counts:
            cve_5y = str(cve_counts[rid])
        elif rid in queried:
            cve_5y = "0"
        else:
            cve_5y = ""

        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "openssf_score": ossf_score,
            "openssf_score_source": ossf_source,
            "cve_count_5y": cve_5y,
            # OSS-Fuzz enrollment: repo_id first (rename-proof), canonical-slug
            # fallback for source rows with a blank repo_id.
            "ossfuzz_enrolled": (
                "True" if rid in fuzz_ids or repo in fuzz_slugs else "False"
            ),
            "bestpractices_badge_id": badges.get(rid, ""),
            "fetched_at": ossf_checked_at,
        })

    # CVE risk score — neutral-anchored. 0 known CVEs → 50 (not the worst-pinned
    # CDF's 78); "none known" is treated as neutral, not proven secure. Repos
    # with ≥1 CVE rank among the above-floor set into (50, 100], worst → 100.
    # Percentiles are computed over the whole top-repo population (github +
    # gitlab together) — platform does not matter.
    def _num(s: str) -> float | None:
        s = (s or "").strip()
        try:
            return float(s) if s else None
        except ValueError:
            return None

    cve_scores = floor_anchored_risk(
        [_num(r.get("cve_count_5y", "")) for r in rows], floor=0.0, anchor=50.0,
    )
    for r, s in zip(rows, cve_scores):
        r["cve_score"] = "" if s is None else round(s, 2)

    # Second pass — openssf risk percentile, then the composite
    # `score` = max(openssf_score_p, cve_score). Max ("worst-of") rather than
    # geometric mean: a bad Scorecard OR a real CVE is on its own enough to flag
    # the repo — neither axis dilutes the other. `max_composite_any` scores off
    # whichever axis is present: a repo with a CVE count but no OpenSSF Scorecard
    # (the common GitLab case — Scorecard's GitLab scan is a separate, gated run)
    # still gets a security score from the CVE axis alone. When both axes are
    # present it is identical to the strict max, so no GitHub row (all of which
    # carry both axes) changes.
    add_percentiles(
        rows,
        pctl_specs=[
            ("openssf_score", False),
        ],
        composite_cols=["openssf_score_p", "cve_score"],
        dim_col="score",
        composite_fn=max_composite_any,
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
                "bestpractices_badge_id",
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
