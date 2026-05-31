#!/usr/bin/env python3
"""Stage 1 of the pipeline — unified per-repo value table.

Pipeline order (each stage feeds the next):
    1. `src.pipeline.value.unify_value_data`       → data/value/value.csv  (this script)
    2. `src.pipeline.eligibility.classify_eligibility` → data/eligibility/eligibility.csv
    3. `src.pipeline.risk.aggregate_risk`        → data/risk/risk.csv

Reads `data/sources/{ecosystem}/results.csv` for each ecosystem (npm, pypi, crates,
cpp), groups packages by canonical `git_url` (or by a per-package synthetic
key for orphans), and writes `data/value/value.csv` with **one row per repo**:

    id, github_repo, gh_repo_id, git_url, valid,
    ecosystems, packages, top_eco, top_eco_pkg,
    top_eco_pct, class, class_npm, class_pypi, class_crates, class_cpp

`github_repo` is normalised to lowercase `owner/repo`. `git_url` is the
canonical clone URL from `results.csv`'s `git` column (lowercased), which
already covers GitHub plus GitLab / Codeberg / Sourcehut / Bitbucket /
custom hosts (sourceware, savannah, etc.). cpp is the unified C/C++
ecosystem (Debian + Homebrew, joined via Repology) -- see
`src/cpp/process_data.py`. The per-repo `valid` column is filled by the
`build_validation` step (a rollup of the GitHub API + `git ls-remote`
validation caches); `gh_repo_id` is set by `verify_git_urls`.

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
which joins per-ecosystem `data/sources/{eco}/eol.csv` with
`data/sources/{eco}/results.csv` to compute per-repo `is_eol` directly.

Usage:
    uv run python -m src.pipeline.value.unify_value_data
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.common.params import assign_value_class

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
# Per-ecosystem inputs (results.csv, top-packages.csv, …) live under
# data/sources/<eco>/ since the data-layout refactor; stage outputs
# (value.csv, overrides.csv) stay under data/value/.
SOURCES_DIR = DATA_DIR / "sources"
OUTPUT_FILE = DATA_DIR / "value" / "value.csv"

# Curated repo overrides — forces the correct `github_repo` for packages
# whose upstream registry metadata points at the wrong repository. This is
# a GAP-CORRECTING layer for bad *upstream* data, not a parsing bug:
# e.g. `@sinclair/typebox`'s latest npm version names a placeholder repo.
# Applied as the LAST step of `aggregate_by_repo` so it survives every
# pipeline re-run; `verify_git_urls` (the next stage) then re-derives the
# corrected repo's `gh_repo_id` from the GitHub API.
OVERRIDES_FILE = DATA_DIR / "value" / "overrides.csv"

ECOSYSTEMS: tuple[str, ...] = ("npm", "pypi", "crates", "cpp")
CLASS_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}

FIELDS = (
    ["id", "github_repo", "gh_repo_id", "git_url", "valid",
     "ecosystems", "packages",
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


def collect_ecosystem(ecosystem: str, data_dir: Path = SOURCES_DIR) -> tuple[list[dict], dict]:
    """Read per-ecosystem files, return per-package rows + funnel stats.

    `data_dir` is the sources root (`data/sources/`); per-ecosystem inputs
    are at `data_dir/<ecosystem>/results.csv` etc.

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

# GitHub paths shaped like `owner/repo` that are NOT repositories — some
# package `git` URLs point at these. `sponsors/<user>` is a sponsorship
# page; `orgs/<org>` an organisation landing page; `topics`/`collections`/
# `marketplace` are discovery pages. Treating any as a repo slug corrupts
# the group's `github_repo` (e.g. github.com/orgs/scikit-build would parse
# to the bogus slug `orgs/scikit-build`).
_GITHUB_RESERVED_NAMESPACES = frozenset(
    {"sponsors", "orgs", "topics", "collections", "marketplace"}
)


def _github_repo_from_url(url: str) -> str:
    """Extract `owner/repo` from a github.com URL; '' otherwise.

    URLs under a GitHub reserved namespace (`sponsors/`, `orgs/`, …) are
    filtered out — they look like `owner/repo` but are not repos, and a
    handful of pypi packages (`attrs`, pydantic, etc.) carry such URLs in
    their `git` field. Treating those as a repo slug would corrupt the
    group's `github_repo`.
    """
    if not url:
        return ""
    m = _GITHUB_URL_RE.match(url.lower())
    if not m:
        return ""
    slug = m.group(1)
    if slug.split("/", 1)[0] in _GITHUB_RESERVED_NAMESPACES:
        return ""
    return slug


def _select_group_github_repo(members: list[dict], git_url: str) -> str:
    """Pick the group's `github_repo`.

    The `git` URL is authoritative: when it yields a parseable GitHub slug
    (reserved namespaces filtered by `_github_repo_from_url`), that slug is
    the repo identity. The `github_repo` field is only a fallback, for
    groups whose `git` URL is a non-GitHub host or absent (orphans).

    Rationale: `git` is backfilled from ecosyste.ms, a purpose-built
    package→repository service; the `github_repo` field is a weaker guess
    that is sometimes flat wrong (e.g. the `influxdb` package tagged
    `simplejson/simplejson`). Cases where the git URL is itself stale or
    points at a fork are corrected by `overrides.csv`, applied
    later in `aggregate_by_repo` — not patched here.

    Fallback (no usable GitHub URL): the most common `github_repo` among
    members, alphabetic tie-break.
    """
    url_slug = _github_repo_from_url(git_url)
    if url_slug:
        return url_slug
    member_repos = [m["github_repo"] for m in members if m.get("github_repo")]
    if not member_repos:
        return ""
    counts = Counter(member_repos)
    top_count = counts.most_common(1)[0][1]
    return min(slug for slug, c in counts.items() if c == top_count)


def _github_git_url(slug: str) -> str:
    """Canonical lowercase `.git` clone URL for a github `owner/repo` slug."""
    return f"https://github.com/{slug}.git"


def load_repo_overrides(path: Path = OVERRIDES_FILE) -> dict[tuple[str, str], dict]:
    """Return {(package, ecosystem): override} from the curated CSV.

    The override file (`overrides.csv`) is the single hand-maintained list of
    manual corrections for packages whose upstream registry metadata is wrong
    (bad data we cannot fix at the source). Each row keys on
    `(package, ecosystem)` and carries up to three corrections plus a required
    free-text `reason`:

    - `github_repo` — force the corrected GitHub slug (lowercased).
    - `git_url`     — force a corrected non-GitHub clone URL (lowercased).
    - `valid`       — manually pin the target's validity (`True`/`False`),
                      consumed later by `build_validation` (NOT applied here).

    Each value is `{"github_repo": str, "git_url": str, "valid": str}` (any
    field may be empty). Rows with a blank `reason` are rejected (these are
    curated and must be explained). A row that sets none of the three is
    skipped (nothing to do). Missing file → no overrides.
    """
    if not path.exists():
        return {}
    out: dict[tuple[str, str], dict] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pkg = (r.get("package") or "").strip()
            eco = (r.get("ecosystem") or "").strip()
            if not (pkg and eco):
                continue
            reason = (r.get("reason") or "").strip()
            if not reason:
                console.print(
                    f"[yellow]overrides.csv: skipping {pkg}/{eco} — blank reason[/yellow]"
                )
                continue
            override = {
                "github_repo": _normalise_repo(r.get("github_repo") or ""),
                "git_url": (r.get("git_url") or "").strip().lower(),
                "valid": (r.get("valid") or "").strip(),
            }
            if not any(override.values()):
                continue
            out[(pkg, eco)] = override
    return out


def apply_repo_overrides(
    aggs: list[dict],
    all_rows: list[dict],
    overrides: dict[tuple[str, str], dict] | None = None,
) -> list[dict]:
    """Force the correct `github_repo` / `git_url` for overridden packages.

    Last step of `aggregate_by_repo`. For every repo aggregate that contains
    a constituent package listed in `overrides.csv`, rewrite the group's
    identity from the curated override — overriding whatever the (wrong)
    registry metadata produced. This is the single chokepoint: it runs after
    grouping and class assignment, and before `verify_git_urls` re-derives
    `gh_repo_id` for the corrected repo.

    Identity rules (per override row):
    - `github_repo` set → set the group's `github_repo` to the slug AND its
      `git_url` to the matching github clone URL (github wins when both
      identity fields are present).
    - else `git_url` set → set the group's `git_url`, leaving `github_repo`
      to its member-derived value.
    The `valid` pin is intentionally NOT applied here — validity is computed
    later by `build_validation`; `load_repo_overrides` only surfaces it.

    `all_rows` is the per-package input (needed to know which packages each
    aggregate contains, since aggregates only store a `packages` count).
    Generic — driven entirely by the override list, no per-package hardcode.

    Matching is by group key: `_group_key` is deterministic and equals the
    aggregate's `group_key` scratch field while it is still present, or the
    aggregate's `git_url` once `_strip_internals` has run — both are the same
    value for non-orphan groups, so this is safe before or after stripping.
    """
    overrides = load_repo_overrides() if overrides is None else overrides
    if not overrides:
        return aggs

    # Map each group key → the override dict, if any constituent package is
    # in the override list. `_group_key` is deterministic, so we can rebuild
    # the same keys from the per-package rows.
    override_by_group: dict[str, dict] = {}
    for row in all_rows:
        ov = overrides.get((row["package"], row["ecosystem"]))
        if ov:
            override_by_group[_group_key(row)] = ov

    for a in aggs:
        # Prefer the scratch `group_key`; fall back to `git_url` (identical
        # for non-orphan groups) so the override still applies if called
        # after `_strip_internals`.
        key = a.get("group_key") or a.get("git_url") or ""
        ov = override_by_group.get(key)
        if not ov:
            continue
        if ov.get("github_repo"):
            a["github_repo"] = ov["github_repo"]
            a["git_url"] = _github_git_url(ov["github_repo"])
        elif ov.get("git_url"):
            a["git_url"] = ov["git_url"]
    return aggs


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
        # `git_url` first (it's the grouping key — one of the members must
        # have it unless this is an orphan group). `github_repo` is then
        # selected to be consistent with `git_url` when possible; see
        # `_select_group_github_repo` for the tie-break logic.
        group_git_url = next((m["git_url"] for m in members if m.get("git_url")), "")
        a: dict = {
            "group_key": key,
            "github_repo": _select_group_github_repo(members, group_git_url),
            "git_url": group_git_url,
            # `valid` is left empty here; build_validation fills the verdict.
            "valid": "",
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

    # Curated repo overrides — LAST transform before sort/write. Forces the
    # correct `github_repo` / `git_url` for packages whose upstream registry
    # metadata names the wrong GitHub repo. Runs here (after class assignment,
    # before sort) so the override slug is what value-data.csv ships and what
    # the downstream `verify_git_urls` step verifies. See OVERRIDES_FILE.
    aggs = apply_repo_overrides(aggs, all_rows)

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


def main() -> None:  # pragma: no cover
    console.print("[bold]Unifying value-pipeline results...[/bold]\n")

    all_rows: list[dict] = []
    stats_per_eco: list[dict] = []
    for ecosystem in ECOSYSTEMS:
        rows, stats = collect_ecosystem(ecosystem)
        all_rows.extend(rows)
        stats_per_eco.append(stats)

    aggs = aggregate_by_repo(all_rows)
    write_value_data(aggs)

    # `gh_repo_id` is populated by `verify_git_urls`, and the `valid` column
    # by `build_validation` — both run after this step by run_value_pipeline.

    _print_funnel_table(stats_per_eco)
    console.print()
    _print_class_table(stats_per_eco)
    console.print()
    _print_eol_table(stats_per_eco)
    console.print()
    _print_repo_class_table(aggs)
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
