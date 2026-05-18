#!/usr/bin/env python3
"""Stage 1 of the pipeline — unified per-repo value table.

Pipeline order (each stage feeds the next):
    1. `src.pipeline.value`       → data/value-data.csv  (this script)
    2. `src.pipeline.eligibility.classify_eligibility` → data/eligibility-data.csv
    3. `src.pipeline.risk.aggregate_risk`        → data/risk-data.csv

Reads `data/{ecosystem}/results.csv` for each ecosystem (npm, pypi, crates,
cpp), groups packages by canonical `git_url` (or by a per-package synthetic
key for orphans), and writes `data/value-data.csv` with **one row per repo**:

    id, github_repo, gh_valid, git_url, git_valid, llm_guess,
    ecosystems, packages, top_eco, top_eco_pkg, top_eco_pct, class,
    class_npm, class_pypi, class_crates, class_cpp

`github_repo` is normalised to lowercase `owner/repo`. `git_url` is the
canonical clone URL from `results.csv`'s `git` column (lowercased), which
already covers GitHub plus GitLab / Codeberg / Sourcehut / Bitbucket /
custom hosts (sourceware, savannah, etc.). cpp is the unified C/C++
ecosystem (Debian + Homebrew, joined via Repology) -- see
`src/cpp/process_data.py`. `gh_valid` / `git_valid` are populated by the
final URL-verification step (GitHub API + `git ls-remote`).

Per-ecosystem class is computed by summing each group's package PR within
the ecosystem, ranking groups by that sum desc, and applying the same
cumulative-share cutoffs as the package-level value pipeline (≤50% A,
≤75% B, ≤90% C, rest D). `class` is the strongest of the per-eco classes
(A < B < C < D). `top_eco_pct = 100 − cumulative_pr_share`, so higher
means closer to the top of the ecosystem; `top_eco` is the ecosystem with
the max percentile and `top_eco_pkg` is the highest-PR package in it.

Rows are sorted by `top_eco_pct` desc so the highest-importance repos
come first.

EOL is intentionally **not** stored here. It's a property of the
eligibility pipeline (license + EOL); see `src.pipeline.eligibility.classify_eligibility`,
which joins per-ecosystem `data/{eco}/eol.csv` with
`data/{eco}/results.csv` to compute per-repo `is_eol` directly.

Usage:
    uv run python -m src.pipeline.value
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.common.params import assign_value_class

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "value-data.csv"
GIT_VALIDITY_CACHE = DATA_DIR / "git" / "urls.csv"

ECOSYSTEMS: tuple[str, ...] = ("npm", "pypi", "crates", "cpp")
CLASS_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}

FIELDS = (
    ["id", "github_repo", "gh_valid", "git_url", "git_valid",
     "llm_guess", "ecosystems", "packages",
     "top_eco", "top_eco_pkg", "top_eco_pct", "class"]
    + [f"class_{e}" for e in ECOSYSTEMS]
)

# Internal scratch keys carried on each aggregate dict during computation.
# Stripped before writing — DictWriter would `extrasaction="ignore"` them
# anyway, but explicit removal keeps the test surface clean.
_INTERNAL_PREFIXES = ("_pkgs_", "_pr_sum_", "_pr_pct_", "_top_pkg_", "group_key")


def _normalise_repo(repo: str) -> str:
    """Lowercase `owner/repo`, stripping whitespace."""
    return repo.strip().lower()


def _read_top_packages(path: Path) -> set[str]:
    """Return the set of package names from a top-packages.csv. Empty if missing."""
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as f:
        return {row["package"] for row in csv.DictReader(f)}


def _read_dep_tree_nodes(path: Path) -> set[str]:
    """Return the set of unique package + dependency nodes in a dep-tree CSV."""
    nodes: set[str] = set()
    if not path.exists():
        return nodes
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            nodes.add(row["package"])
            nodes.add(row["dependency"])
    return nodes


def _read_eol_index(path: Path) -> dict[str, bool]:
    """Return {package: is_eol} from a per-ecosystem eol.csv. {} if missing."""
    if not path.exists():
        return {}
    idx: dict[str, bool] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            idx[r["package"]] = r["is_eol"] == "True"
    return idx


def collect_ecosystem(ecosystem: str, data_dir: Path = DATA_DIR) -> tuple[list[dict], dict]:
    """Read per-ecosystem files, return per-package rows + funnel stats.

    `git_url` for each row comes directly from `results.csv`'s `git`
    column (lowercased) — that column is already populated and is the
    canonical clone URL, so no separate join with `git.csv` is needed.
    """
    eco_dir = data_dir / ecosystem
    top_path = eco_dir / "top-packages.csv"
    deps_path = eco_dir / "dependency-tree.csv"
    results_path = eco_dir / "results.csv"
    eol_path = eco_dir / "eol.csv"

    top_set = _read_top_packages(top_path)
    dep_nodes = _read_dep_tree_nodes(deps_path)
    # "After dep tree" = top packages plus their transitive deps. Some top
    # packages have no declared deps and no inbound deps, so they don't appear
    # in any dep-tree edge -- union with `top_set` to keep the count monotonic.
    after_deps = len(top_set | dep_nodes)
    top_count = len(top_set)
    eol_idx = _read_eol_index(eol_path)

    rows: list[dict] = []
    if results_path.exists():
        with open(results_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                pkg = r["package"]
                rows.append({
                    "package": pkg,
                    "ecosystem": ecosystem,
                    "github_repo": _normalise_repo(r.get("github_repo", "")),
                    "git_url": (r.get("git") or "").strip().lower(),
                    "llm_guess": (r.get("llm_guess") or "").strip(),
                    "pagerank": r.get("pagerank", ""),
                    "value_class": r.get("value_class", ""),
                    "is_eol": eol_idx.get(pkg, False),
                })

    classes = Counter(r["value_class"] for r in rows if r["value_class"])
    with_gh = sum(1 for r in rows if r["github_repo"])
    with_git = sum(1 for r in rows if r["git_url"])
    eol_count = sum(1 for r in rows if r["is_eol"])

    ab_rows = [r for r in rows if r["value_class"] in ("A", "B")]
    ab_with_gh = sum(1 for r in ab_rows if r["github_repo"])
    ab_with_git = sum(1 for r in ab_rows if r["git_url"])
    ab_eol = sum(1 for r in ab_rows if r["is_eol"])

    stats = {
        "ecosystem": ecosystem,
        "top": top_count,
        "deps_unique": after_deps,
        "results": len(rows),
        "with_gh": with_gh,
        "with_git": with_git,
        "gh_pct": (100.0 * with_gh / len(rows)) if rows else 0.0,
        "git_pct": (100.0 * with_git / len(rows)) if rows else 0.0,
        "classes": classes,
        "ab_total": len(ab_rows),
        "ab_with_gh": ab_with_gh,
        "ab_with_git": ab_with_git,
        "ab_gh_pct": (100.0 * ab_with_gh / len(ab_rows)) if ab_rows else 0.0,
        "ab_git_pct": (100.0 * ab_with_git / len(ab_rows)) if ab_rows else 0.0,
        "eol_covered": bool(eol_idx),
        "eol_count": eol_count,
        "ab_eol": ab_eol,
    }
    return rows, stats


def _group_key(row: dict) -> str:
    """`git_url` if non-empty (subsumes github grouping since every
    github_repo maps to a deterministic github.com `.git` URL); otherwise
    a synthetic per-package orphan key.

    Grouping by `git_url` instead of `github_repo` collapses non-GitHub
    upstreams that ship multiple ecosystem packages (e.g. gstreamer-rs
    publishes 10 Cargo crates from one gitlab.freedesktop.org repo) into
    a single repo row.
    """
    git_url = row.get("git_url") or ""
    if git_url:
        return git_url
    return f"__orphan__:{row['ecosystem']}:{row['package']}"


_GITHUB_URL_RE = re.compile(r"^https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$")


def _github_repo_from_url(url: str) -> str:
    """Extract `owner/repo` from a github.com URL; '' otherwise.

    `github.com/sponsors/<user>` is filtered out — that's a sponsorship
    page, not a repo namespace, and a handful of pypi packages (`attrs`,
    pydantic, etc.) carry such URLs in their `git` field. Treating those
    as a repo slug would corrupt the group's `github_repo`.
    """
    if not url:
        return ""
    m = _GITHUB_URL_RE.match(url.lower())
    if not m:
        return ""
    slug = m.group(1)
    if slug.startswith("sponsors/"):
        return ""
    return slug


def _select_group_github_repo(members: list[dict], git_url: str) -> str:
    """Pick the group's `github_repo`.

    Strategy: take the most common `github_repo` among members
    (alphabetic tiebreak), then promote the url-derived slug when it's
    among the tied winners.

    Why "most common" and not "first non-empty": some upstream rows
    disagree on what the `github_repo` should be (~90 such rows in
    pypi+cpp). When most members agree on slug X but a stray row says
    Y, we want X — and "first non-empty" picks whatever the iteration
    order happened to put first. Ties are broken alphabetically for
    determinism, except when the url-derived slug is one of the tied
    candidates: then we prefer it (e.g. for a 1-vs-1 fork-vs-main split,
    the URL is the source of truth).

    Real cases this disambiguates correctly:
      - opentelemetry-python-contrib (22 right, 1 wrong) → contrib wins
      - python/typeshed (23) vs typeshed-internal/stub_uploader (15)
        on a stub_uploader URL → typeshed wins (majority)
      - attrs/python-attrs (1) vs sponsors/hynek (1) on a sponsors URL
        → python-attrs wins (URL is filtered, alphabetic tiebreak)
    """
    url_slug = _github_repo_from_url(git_url)
    member_repos = [m["github_repo"] for m in members if m.get("github_repo")]
    if not member_repos:
        return ""
    counts = Counter(member_repos)
    top_count = counts.most_common(1)[0][1]
    tied = [slug for slug, c in counts.items() if c == top_count]
    if url_slug and url_slug in tied:
        return url_slug
    return min(tied)


def _strip_internals(a: dict) -> dict:
    """Drop scratch keys before writing."""
    return {k: v for k, v in a.items() if not any(k.startswith(p) for p in _INTERNAL_PREFIXES)}


def aggregate_by_repo(all_rows: list[dict], *, drop_d_class: bool = False) -> list[dict]:
    """Collapse per-package rows into one row per repo (or per orphan).

    Per-ecosystem PR sum → cumulative-share ranking → A/B/C/D, plus
    top_eco / top_eco_pkg / top_eco_pct, the cross-ecosystem `class`
    (strongest), and the comma-separated `ecosystems` list. Rows are
    sorted by `top_eco_pct` desc.

    `drop_d_class` (default False) keeps all classes — value-data.csv is
    the complete long-tail table (A/B/C/D). Pass True to drop the D tail.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        groups[_group_key(r)].append(r)

    aggs: list[dict] = []
    for key, members in groups.items():
        # Union llm_guess across all packages in the group — if ANY constituent
        # package's URL came from the LLM data, surface it.
        # (eco_guess is tracked per-package in results.csv but not surfaced at
        #  the repo aggregate level since it's noisy and rarely actionable.)
        llm_tags: set[str] = set()
        for m in members:
            for tag in (m.get("llm_guess") or "").split(","):
                tag = tag.strip()
                if tag:
                    llm_tags.add(tag)
        # `git_url` first (it's the grouping key — one of the members must
        # have it unless this is an orphan group). `github_repo` is then
        # selected to be consistent with `git_url` when possible; see
        # `_select_group_github_repo` for the tie-break logic.
        group_git_url = next((m["git_url"] for m in members if m.get("git_url")), "")
        a: dict = {
            "group_key": key,
            "github_repo": _select_group_github_repo(members, group_git_url),
            "git_url": group_git_url,
            "llm_guess": ",".join(sorted(llm_tags)),
            "packages": len(members),
        }
        present_ecos: list[str] = []
        for eco in ECOSYSTEMS:
            eco_rows = [m for m in members if m["ecosystem"] == eco]
            a[f"_pkgs_{eco}"] = len(eco_rows)
            a[f"_pr_sum_{eco}"] = sum(
                float(m["pagerank"]) for m in eco_rows if m.get("pagerank")
            )
            if eco_rows:
                present_ecos.append(eco)
                top_pkg = max(
                    eco_rows, key=lambda m: float(m.get("pagerank") or 0),
                )
                a[f"_top_pkg_{eco}"] = top_pkg["package"]
        a["ecosystems"] = ",".join(present_ecos)
        aggs.append(a)

    # Per ecosystem: rank by PR sum desc, compute cumulative share, assign class.
    # `_pr_pct_<eco>` = 100 - cum_share so higher is better; used to pick top_eco.
    for eco in ECOSYSTEMS:
        present = [a for a in aggs if a[f"_pkgs_{eco}"] > 0]
        present.sort(key=lambda a: a[f"_pr_sum_{eco}"], reverse=True)
        total = sum(a[f"_pr_sum_{eco}"] for a in present)
        cum = 0.0
        for a in present:
            cum += a[f"_pr_sum_{eco}"]
            cum_pct = (cum / total * 100.0) if total else 0.0
            a[f"class_{eco}"] = assign_value_class(cum_pct / 100.0)
            a[f"_pr_pct_{eco}"] = 100.0 - cum_pct
        for a in aggs:
            a.setdefault(f"class_{eco}", "")

    # top_eco / top_eco_pkg / top_eco_pct + cross-eco strongest `class`.
    # Every group has at least one package in some ecosystem (it wouldn't
    # exist otherwise), so `pcts` and `present_classes` are always non-empty.
    # Tied percentiles resolve in ECOSYSTEMS order (npm wins ties).
    for a in aggs:
        pcts = {e: a[f"_pr_pct_{e}"] for e in ECOSYSTEMS if f"_pr_pct_{e}" in a}
        top = max(pcts, key=pcts.get)
        a["top_eco"] = top
        a["top_eco_pkg"] = a.get(f"_top_pkg_{top}", "")
        a["top_eco_pct"] = round(pcts[top], 4)
        present_classes = [a[f"class_{e}"] for e in ECOSYSTEMS if a[f"class_{e}"]]
        a["class"] = min(present_classes, key=lambda c: CLASS_RANK[c])

    # value-data.csv stores all classes A/B/C/D — the complete long-tail
    # table. The risk pipeline filters to its own scope (A/B by default,
    # via settings.json risk_input.value_classes); eligibility runs after
    # risk. `drop_d_class` is retained for callers that want the ABC subset.
    if drop_d_class:
        aggs = [a for a in aggs if a.get("class") in ("A", "B", "C")]

    # Sort by top_eco_pct desc. Every group has a numeric percentile (set
    # above), so no special handling for missing values is needed; ties
    # broken by repo name for stability.
    aggs.sort(key=lambda a: (-a["top_eco_pct"], a["github_repo"] or a["group_key"]))
    for i, a in enumerate(aggs, 1):
        a["id"] = i
    return [_strip_internals(a) for a in aggs]


def write_value_data(aggs: list[dict], path: Path = OUTPUT_FILE) -> None:
    """Write the per-repo aggregate to a CSV using the canonical schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_ALL,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(aggs)


# ── display helpers ──────────────────────────────────────────────────────────
# These only build rich tables; they have no logic and are excluded from
# coverage requirements.

def _print_funnel_table(stats_per_eco: list[dict]) -> None:  # pragma: no cover
    table = Table(title="[bold]Value pipeline funnel[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Ecosystem", style="bold")
    for col in ("Top", "After deps", "Results", "With GH", "GH %", "With Git", "Git %"):
        table.add_column(col, justify="right")

    tot = {"top": 0, "deps_unique": 0, "results": 0, "with_gh": 0, "with_git": 0}
    for s in stats_per_eco:
        table.add_row(
            s["ecosystem"], f"{s['top']:,}", f"{s['deps_unique']:,}",
            f"{s['results']:,}", f"{s['with_gh']:,}", f"{s['gh_pct']:.0f}%",
            f"{s['with_git']:,}", f"{s['git_pct']:.0f}%",
        )
        for k in tot:
            tot[k] += s[k]

    table.add_section()
    gh_pct = (100.0 * tot["with_gh"] / tot["results"]) if tot["results"] else 0.0
    git_pct = (100.0 * tot["with_git"] / tot["results"]) if tot["results"] else 0.0
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{tot['top']:,}[/bold]", f"[bold]{tot['deps_unique']:,}[/bold]",
        f"[bold]{tot['results']:,}[/bold]", f"[bold]{tot['with_gh']:,}[/bold]",
        f"[bold]{gh_pct:.0f}%[/bold]",
        f"[bold]{tot['with_git']:,}[/bold]", f"[bold]{git_pct:.0f}%[/bold]",
    )
    console.print(table)


def _print_eol_table(stats_per_eco: list[dict]) -> None:  # pragma: no cover
    table = Table(title="[bold]EOL coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Ecosystem", style="bold")
    table.add_column("eol.csv", justify="center")
    table.add_column("EOL total", justify="right", style="red")
    table.add_column("EOL A+B", justify="right", style="red")

    tot_eol, tot_ab_eol = 0, 0
    for s in stats_per_eco:
        table.add_row(
            s["ecosystem"],
            "[green]✓[/green]" if s["eol_covered"] else "[dim]–[/dim]",
            f"{s['eol_count']:,}", f"{s['ab_eol']:,}",
        )
        tot_eol += s["eol_count"]
        tot_ab_eol += s["ab_eol"]
    table.add_section()
    table.add_row("[bold]Total[/bold]", "",
                  f"[bold]{tot_eol:,}[/bold]", f"[bold]{tot_ab_eol:,}[/bold]")
    console.print(table)


def _print_class_table(stats_per_eco: list[dict]) -> None:  # pragma: no cover
    table = Table(title="[bold]Value class distribution[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Ecosystem", style="bold")
    for cls in "ABCD":
        table.add_column(cls, justify="right")
    table.add_column("Total", justify="right")
    table.add_column("A+B GH", justify="right")
    table.add_column("A+B Git", justify="right")

    totals = Counter()
    ab_total, ab_gh, ab_git = 0, 0, 0
    for s in stats_per_eco:
        row = [s["ecosystem"]]
        eco_total = sum(s["classes"].values())
        for cls in "ABCD":
            n = s["classes"].get(cls, 0)
            totals[cls] += n
            row.append(f"{n:,}")
        row.append(f"{eco_total:,}")
        row.append(f"{s['ab_gh_pct']:.0f}%")
        row.append(f"{s['ab_git_pct']:.0f}%")
        ab_total += s["ab_total"]
        ab_gh += s["ab_with_gh"]
        ab_git += s["ab_with_git"]
        table.add_row(*row)

    grand = sum(totals.values())
    grand_ab_gh_pct = (100.0 * ab_gh / ab_total) if ab_total else 0.0
    grand_ab_git_pct = (100.0 * ab_git / ab_total) if ab_total else 0.0
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        *(f"[bold]{totals[cls]:,}[/bold]" for cls in "ABCD"),
        f"[bold]{grand:,}[/bold]",
        f"[bold]{grand_ab_gh_pct:.0f}%[/bold]",
        f"[bold]{grand_ab_git_pct:.0f}%[/bold]",
    )
    console.print(table)


def _print_repo_class_table(aggs: list[dict]) -> None:  # pragma: no cover
    table = Table(title="[bold]Repo class distribution[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("class")
    for eco in ECOSYSTEMS:
        table.add_column(eco, justify="right")
    table.add_column("strongest", justify="right", style="bold")
    for cls in ("A", "B", "C", "D"):
        row = [cls]
        for eco in ECOSYSTEMS:
            n = sum(1 for a in aggs if a[f"class_{eco}"] == cls)
            row.append(f"{n:,}")
        n_strongest = sum(1 for a in aggs if a["class"] == cls)
        row.append(f"{n_strongest:,}")
        table.add_row(*row)
    console.print(table)


# ── Git URL verifier ────────────────────────────────────────────────────────
#
# Final value-pipeline step. Two strategies, chosen per URL:
#
#   1. github_repo present → call `src.github.fetch_repo_owner_data.fetch_and_persist`
#      which hits `/repos/{owner}/{repo}` via the GitHub API. Persists
#      richer metadata to `data/github/repos.csv` (license, owner, stars,
#      …) so downstream pipelines (eligibility) don't need to re-fetch.
#   2. non-github canonical git URL → `git ls-remote --exit-code` against
#      the URL itself. Persists OK/FAIL to `data/git/urls.csv`.
#
# Both layers are TTL'd by `GIT_URL_TTL_DAYS`. A repo without any URL gets
# `gh_valid="" / git_valid=""` (unknown).

GIT_URL_TTL_DAYS = 365
LS_REMOTE_TIMEOUT = 25
LSREMOTE_PARALLEL = 5

_VALIDITY_FIELDS = ["url", "valid", "method", "checked_at"]

# Hosts that don't support the unauthenticated `git://` daemon — keep / fall
# back to https://. Most modern GitLab installs, Bitbucket, KDE Invent,
# Phabricator, Codeberg, OpenLDAP, postgresql.org, qemu.org, etc.
HTTPS_ONLY_HOST_PREFIXES: tuple[str, ...] = (
    "gitlab.",                  # GitLab Enterprise / .com
    "invent.kde.org",
    "salsa.debian.org",
    "code.videolan.org",
    "bitbucket.org",
    "dev.gnupg.org",            # Phabricator
    "chromium.googlesource.com",  # gitiles
    "aomedia.googlesource.com",
    # Hosts that historically supported git:// but disabled it later:
    "git.openldap.org",
    "git.postgresql.org",
    "codeberg.org",
    "git.libssh.org",
    "git.qemu.org",
    "pagure.io",
)


def _host(url: str) -> str:
    """Extract the host component from a URL, or '' if not parseable."""
    if "//" not in url:
        return ""
    return url.split("//", 1)[1].split("/", 1)[0]


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_validity_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            u = (r.get("url") or "").strip()
            if u:
                out[u] = r
    return out


def _save_validity_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_VALIDITY_FIELDS,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        for u in sorted(cache.keys()):
            w.writerow(cache[u])
    os.replace(tmp, path)


def _is_fresh(checked_at: str) -> bool:
    if not checked_at:
        return False
    try:
        when = dt.datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=GIT_URL_TTL_DAYS)
        return when >= cutoff
    except Exception:
        return False


def _lsremote_pass(urls: list[str]) -> dict[str, bool]:
    """Definitive `git ls-remote` check for the ambiguous URLs."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
        "GIT_HTTP_LOW_SPEED_LIMIT": "500",
        "GIT_HTTP_LOW_SPEED_TIME": "20",
    }

    def _check(url: str) -> tuple[str, bool]:
        try:
            r = subprocess.run(
                ["git", "ls-remote", "--exit-code", url, "HEAD"],
                capture_output=True, timeout=LS_REMOTE_TIMEOUT,
                env=env, text=True,
            )
            return url, (r.returncode == 0 and bool(r.stdout.strip()))
        except subprocess.TimeoutExpired:
            return url, False
        except Exception:
            return url, False

    out: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=LSREMOTE_PARALLEL) as ex:
        futs = {ex.submit(_check, u): u for u in urls}
        for fut in as_completed(futs):
            u, ok = fut.result()
            out[u] = ok
    return out


def _canonicalize_git_url(url: str) -> str:
    """Rewrite known-wrong git URL patterns to their canonical clone form.

    Pipeline of rewrites — each rule UPDATES `u` rather than returning
    early, so post-processing (github-strip, https→git:// conversion)
    runs unconditionally at the end.

    Returns the cleaned URL, or `""` if there's no git equivalent
    (non-git VCS like Mercurial, dead servers, github URL — github URLs
    are tracked via the `github_repo` column).
    """
    u = (url or "").strip()
    if not u:
        return ""

    # ── Pattern rewrites (mechanical) ─────────────────────────────────
    # Strip gitweb display-suffixes (;a=summary, ;a=tree, etc.)
    u = re.sub(r";[a-z]=[\w]+(;[a-z]=[\w]+)*\b", "", u)

    # Savannah cgit/git web view → git:// (HTTPS smart-http is broken/slow,
    # git:// works instantly; path drops the `/git/` prefix).
    m = re.match(r"^https?://git\.savannah\.(gnu|nongnu)\.org/(?:cgit|git)/(.+)$", u)
    if m:
        u = f"git://git.savannah.{m.group(1)}.org/{m.group(2)}"

    # kernel.org cgit web view → smart-http git endpoint at /pub/scm/...
    m = re.match(r"^https?://git\.kernel\.org/cgit/(.+?)(?:\.git/?)?$", u)
    if m:
        u = f"https://git.kernel.org/pub/scm/{m.group(1)}.git"

    # GnuPG gitweb → use gpg/* GitHub mirror (their smart-http is broken)
    m = re.match(r"^https?://git\.gnupg\.org/cgi-bin/gitweb\.cgi\?p=([\w.-]+)\.git$", u)
    if m:
        u = f"https://github.com/gpg/{m.group(1)}.git"

    # GnuPG direct .git URL → also use gpg/* GitHub mirror
    m = re.match(r"^https?://git\.gnupg\.org/([\w.-]+)\.git$", u)
    if m:
        u = f"https://github.com/gpg/{m.group(1)}.git"

    # Sourceware project landing page → git endpoint at /git/X.git
    m = re.match(r"^https?://sourceware\.org/([a-z][\w-]*)/?$", u)
    if m and m.group(1) not in ("git", "cgit", "elfutils-doc"):
        u = f"https://sourceware.org/git/{m.group(1)}.git"

    # VideoLAN gitweb → code.videolan.org GitLab
    m = re.match(r"^https?://git\.videolan\.org/\?p=([\w.-]+)\.git", u)
    if m:
        u = f"https://code.videolan.org/videolan/{m.group(1)}.git"

    # Linux-NFS gitweb (?p=user/proj.git) → git:// form
    m = re.match(r"^https?://git\.linux-nfs\.org/\?p=([^/]+)/([\w.-]+)\.git", u)
    if m:
        u = f"git://git.linux-nfs.org/projects/{m.group(1)}/{m.group(2)}.git"

    # freedesktop anongit → gitlab.freedesktop.org
    m = re.match(r"^https?://anongit\.freedesktop\.org/git/(.+)\.git$", u)
    if m:
        u = f"https://gitlab.freedesktop.org/{m.group(1)}.git"

    # OpenLDAP archive download URL → main repo
    if u.startswith("https://git.openldap.org/openldap/openldap/-/archive/"):
        u = "https://git.openldap.org/openldap/openldap.git"

    # Xiph old git.xiph.org → gitlab.xiph.org
    m = re.match(r"^https?://git\.xiph\.org/([\w.-]+)\.git$", u)
    if m:
        u = f"https://gitlab.xiph.org/xiph/{m.group(1)}.git"

    # SourceForge legacy <project>.git.sourceforge.net → modern git.code.sf.net
    m = re.match(r"^https?://([\w-]+)\.git\.sourceforge\.net/?$", u)
    if m:
        u = f"https://git.code.sf.net/p/{m.group(1)}/code"

    # GitLab API URL (graphviz et al.) — not a git endpoint
    if u.startswith("https://gitlab.com/api/v4/") or "/api/v4/projects/" in u:
        u = "https://gitlab.com/graphviz/graphviz.git" if "4207231" in u else ""

    # Postgres canonical missing .git suffix
    if u == "https://git.postgresql.org/git/postgresql":
        u = "https://git.postgresql.org/git/postgresql.git"

    # libpthread-stubs path mismatch on gitlab.freedesktop.org
    if u == "https://gitlab.freedesktop.org/xorg/lib/libpthread-stubs.git":
        u = "https://gitlab.freedesktop.org/xorg/lib/pthread-stubs.git"

    # ── Post-processing (always runs) ─────────────────────────────────
    if not u:
        return ""

    # Non-git VCS — blank out, no canonical git exists
    if u.startswith(("https://hg.", "http://hg.",
                     "https://foss.heptapod.net/",
                     "https://code.launchpad.net/",
                     "https://core.tcl-lang.org/",
                     "https://gmplib.org/repo/",
                     "https://bitbucket.org/stoneleaf/",
                     "https://www.bytereef.org/")):
        return ""

    # Github URLs are tracked via the `github_repo` column already;
    # `git_url` is reserved for non-github canonicals. Strip them.
    if re.match(r"^https?://github\.com/", u):
        return ""

    # Standardise on git:// scheme for non-github URLs unless the host
    # explicitly disabled the unauthenticated git daemon — those hosts
    # are listed at module level in HTTPS_ONLY_HOST_PREFIXES.
    host = _host(u)
    https_only = any(host == h or host.startswith(h)
                     for h in HTTPS_ONLY_HOST_PREFIXES)

    if https_only:
        # Convert any scheme to https:// for these hosts
        if u.startswith("git://"):
            return "https://" + u[len("git://"):]
        if u.startswith("http://"):
            return "https://" + u[len("http://"):]
        return u
    # Otherwise prefer git:// — it's faster and tends to work on traditional
    # git daemons (Savannah, kernel.org, sourceware, sourceforge, …).
    if u.startswith("https://"):
        return "git://" + u[len("https://"):]
    if u.startswith("http://"):
        return "git://" + u[len("http://"):]
    return u


def _verify_non_github(urls: list[str],
                       cache_path: Path = GIT_VALIDITY_CACHE,
                       force: bool = False) -> dict[str, bool]:
    """`git ls-remote` verification for non-github URLs, with TTL cache."""
    cache = _load_validity_cache(cache_path)
    now = _now_iso()

    fresh: dict[str, bool] = {}
    to_check: list[str] = []
    for u in urls:
        c = cache.get(u)
        if not force and c and _is_fresh(c.get("checked_at", "")):
            fresh[u] = (c.get("valid", "").lower() == "true")
        else:
            to_check.append(u)

    console.print(
        f"  [dim]non-github URLs:[/dim] {len(urls):,} unique  "
        f"[green]{len(fresh):,} cached[/green]  "
        f"[yellow]{len(to_check):,} to verify[/yellow]"
    )

    new_results: dict[str, bool] = {}
    if to_check:
        t0 = time.monotonic()
        results = _lsremote_pass(to_check)
        ok = sum(1 for v in results.values() if v)
        console.print(
            f"  [dim]ls-remote ({time.monotonic()-t0:.1f}s):[/dim] "
            f"valid={ok}/{len(to_check)}"
        )
        for u, v in results.items():
            new_results[u] = v
            cache[u] = {"url": u, "valid": str(bool(v)),
                        "method": "ls-remote", "checked_at": now}
        _save_validity_cache(cache_path, cache)
    return {**fresh, **new_results}


def verify_urls_in_aggregates(aggs: list[dict],
                              force: bool = False) -> tuple[list[dict], dict]:
    """Populate `gh_valid` and `git_valid` on every row. Two strategies:

      • github_repo set → trigger `fetch_repo_owner_data.fetch_and_persist`,
        then look up `valid` in `data/github/repos.csv` (→ `gh_valid`).
      • non-github git_url → `git ls-remote`, cached in `data/git/urls.csv`
        (→ `git_valid`).

    Returns (aggs, summary_stats). Mutates `aggs` in place.
    """
    # Lazy import: keeps CLI startup snappy when not actually verifying.
    from src.github.fetch_repo_owner_data import (
        fetch_and_persist,
        REPOS_OUT as GH_REPOS_FILE,
        owners_from_repos,
    )

    # First — mechanical canonicalisation of known wrong URL patterns
    # (cgit→git, gitweb→git, anongit→gitlab, etc.). This recovers the
    # majority of "valid project, wrong URL" cases without any network IO.
    rewritten = 0
    blanked = 0
    for a in aggs:
        old = (a.get("git_url") or "").strip()
        new = _canonicalize_git_url(old)
        if new != old:
            a["git_url"] = new
            if not new:
                blanked += 1
            else:
                rewritten += 1
    console.rule("[bold cyan]value/verify URLs")
    if rewritten or blanked:
        console.print(f"  [dim]URL canonicalisation:[/dim] "
                      f"rewritten=[cyan]{rewritten}[/cyan] "
                      f"blanked=[red]{blanked}[/red] (non-git VCS)")

    # Split URLs: github vs non-github
    github_repos: set[str] = set()
    nongithub_urls: set[str] = set()
    for a in aggs:
        gh = (a.get("github_repo") or "").strip().lower()
        gu = (a.get("git_url") or "").strip()
        if gh and "/" in gh:
            github_repos.add(gh)
        elif gu:
            nongithub_urls.add(gu)

    console.print(f"  github repos:    {len(github_repos):,}")
    console.print(f"  non-github URLs: {len(nongithub_urls):,}")

    # --- GitHub side ---
    repos_list = sorted(github_repos)
    owners_list = owners_from_repos(repos_list)
    fetch_and_persist(
        repos=repos_list,
        owners=owners_list,
        target="repos",  # don't fetch users here — that's eligibility's job
        force=force,
        quiet=False,
    )
    # Read back validity
    gh_valid: dict[str, bool] = {}
    if GH_REPOS_FILE.exists():
        with open(GH_REPOS_FILE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                slug = (r.get("repo") or "").strip().lower()
                if slug:
                    gh_valid[slug] = (r.get("valid","").lower() == "true")

    # --- Non-github side ---
    nongh_valid = _verify_non_github(sorted(nongithub_urls), force=force)

    # --- Annotate gh_valid + git_valid ---
    # gh_valid: True/False from the GitHub API; "" if no github_repo.
    # git_valid: True/False from `git ls-remote`; "" if no git_url *or*
    #   if the row already has a github_repo (only one side is verified —
    #   the same elif intent as where URLs were queued above).
    # Note: `_canonicalize_git_url` already strips github.com URLs from
    # `git_url` (they're tracked via `github_repo`), so we never need to
    # cross-reference gh_valid from git_valid.
    invalid_examples: list[tuple[str, str, str]] = []  # (kind, key, url)
    for a in aggs:
        gh = (a.get("github_repo") or "").strip().lower()
        gu = (a.get("git_url") or "").strip()
        has_gh = bool(gh and "/" in gh)

        a["gh_valid"] = gh_valid.get(gh, False) if has_gh else ""
        a["git_valid"] = nongh_valid.get(gu, False) if gu and not has_gh else ""

        # Track invalids for the summary table. After the fix, gh_valid
        # and git_valid are never both False on the same row — only the
        # verified side can fail.
        if a["gh_valid"] is False:
            invalid_examples.append(("gh", gh, gu))
        elif a["git_valid"] is False:
            invalid_examples.append(("git", a.get("top_eco_pkg", ""), gu))

    # Stats: a row is "valid" if BOTH columns are True (or one is True and the
    # other is empty). A row is "invalid" if either is explicitly False.
    valid_rows = sum(1 for a in aggs
                     if (a["gh_valid"] is True or a["gh_valid"] == "") and
                        (a["git_valid"] is True or a["git_valid"] == "") and
                        (a["gh_valid"] is True or a["git_valid"] is True))
    invalid_rows = sum(1 for a in aggs
                       if a["gh_valid"] is False or a["git_valid"] is False)
    no_url = sum(1 for a in aggs
                 if a["gh_valid"] == "" and a["git_valid"] == "")
    return aggs, {
        "valid": valid_rows, "invalid": invalid_rows, "no_url": no_url,
        "invalid_examples": invalid_examples[:30],
    }


def _print_git_validity_table(stats: dict) -> None:  # pragma: no cover
    """Show counts + sample of invalid URLs."""
    total = stats["valid"] + stats["invalid"] + stats["no_url"]
    table = Table(title="[bold]Git URL Validity[/bold]",
                  header_style="bold dim", padding=(0, 1))
    table.add_column(""); table.add_column("Rows", justify="right")
    table.add_column("%", justify="right")

    def row(label, n, color=""):
        pct = f"{100*n/total:.1f}%" if total else "-"
        if color:
            table.add_row(f"[{color}]{label}[/{color}]",
                          f"[{color}]{n:,}[/{color}]",
                          f"[{color}]{pct}[/{color}]")
        else:
            table.add_row(label, f"{n:,}", pct)

    row("Valid",        stats["valid"],   "green")
    row("Invalid",      stats["invalid"], "red")
    row("No git URL",   stats["no_url"],  "dim")
    table.add_section()
    table.add_row("[bold]Total[/bold]", f"[bold]{total:,}[/bold]", "")
    console.print(table)

    if stats["invalid_examples"]:
        sample = Table(title=f"[red]Invalid URLs[/red] (first {len(stats['invalid_examples'])})",
                       header_style="bold dim", padding=(0, 1))
        sample.add_column("kind", width=4); sample.add_column("repo / pkg", min_width=22)
        sample.add_column("git_url")
        for kind, key, url in stats["invalid_examples"]:
            sample.add_row(kind, key, url)
        console.print(sample)


def main() -> None:  # pragma: no cover
    console.print("[bold]Unifying value-pipeline results...[/bold]\n")

    all_rows: list[dict] = []
    stats_per_eco: list[dict] = []
    for ecosystem in ECOSYSTEMS:
        rows, stats = collect_ecosystem(ecosystem)
        all_rows.extend(rows)
        stats_per_eco.append(stats)

    aggs = aggregate_by_repo(all_rows)

    # Final step: verify every URL accepts a git connection.
    # GitHub repos go through the GitHub API (richer metadata persisted to
    # data/github/repos.csv); non-github URLs go through `git ls-remote`.
    aggs, vstats = verify_urls_in_aggregates(aggs)

    write_value_data(aggs)

    _print_funnel_table(stats_per_eco)
    console.print()
    _print_class_table(stats_per_eco)
    console.print()
    _print_eol_table(stats_per_eco)
    console.print()
    _print_repo_class_table(aggs)
    console.print()
    _print_git_validity_table(vstats)
    console.print()
    n_grouped = sum(1 for a in aggs if a["github_repo"])
    n_orphan = len(aggs) - n_grouped
    console.print(
        f"[dim]Written {len(aggs):,} repo rows "
        f"({n_grouped:,} github groups + {n_orphan:,} orphan packages) "
        f"→ {OUTPUT_FILE}[/dim]"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
