"""One-shot migration: lizard cognitive + cyclo-halstead → data/git/lizard.csv.

Reads two sha-pinned raw metric files and rewrites them in long format
(`repo, repo_id, commit_sha, metric, value, checked_at`) using
`src.git.long_format.upsert_snapshot`.

Source files (read-only):
  - data/github/git/cognitive.csv      (~895 rows, cognitive complexity)
  - data/github/git/cyclo-halstead.csv (~21 rows, cyclomatic + halstead)

Destination:
  - data/git/lizard.csv (created fresh; aborts if it already exists)

Run:
  uv run python -m src.git.migrations.migrate_lizard
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.git.long_format import read as read_long
from src.git.long_format import upsert_snapshot

ROOT = Path(__file__).resolve().parents[3]
COGNITIVE_CSV = ROOT / "data" / "github" / "git" / "cognitive.csv"
CYCLO_CSV = ROOT / "data" / "github" / "git" / "cyclo-halstead.csv"
ELIGIBILITY_CSV = ROOT / "data" / "eligibility-data.csv"
DEST_CSV = ROOT / "data" / "git" / "lizard.csv"

# Metric columns to emit per source. Order is intentional: shared `files`
# first so it gets upserted (overwritten) by the cyclo-halstead pass when
# both sources cover the same (repo, sha).
COGNITIVE_METRICS = ["files", "cognitive_total", "cognitive_avg", "cognitive_max"]
CYCLO_METRICS = [
    "files",
    "cyclomatic_total",
    "cyclomatic_avg",
    "cyclomatic_max",
    "halstead_volume",
    "halstead_difficulty",
    "halstead_effort",
    "halstead_bugs",
    "maintainability_index",
]


def load_repo_ids(path: Path) -> dict[str, str]:
    """Map `repo` → `repo_id` from eligibility-data.csv. Empty string if missing."""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            repo = (row.get("repo") or "").strip()
            repo_id = (row.get("repo_id") or "").strip()
            if repo:
                out[repo] = repo_id
    return out


def _parse_value(raw: str):
    """Best-effort parse of a CSV cell into int/float/str. Empty stays None."""
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None
    # int first (no dot), then float, else str
    try:
        if "." not in s and "e" not in s and "E" not in s:
            return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def migrate_source(
    src_path: Path,
    metric_cols: list[str],
    repo_ids: dict[str, str],
    dest_path: Path,
) -> tuple[int, set[tuple[str, str]]]:
    """Apply one source CSV to the destination via upsert_snapshot.

    Returns (rows_read, set_of_(repo, sha) pairs covered).
    """
    pairs: set[tuple[str, str]] = set()
    rows_read = 0
    with src_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_read += 1
            repo = (row.get("repo") or "").strip()
            sha = (row.get("analyzed_sha") or "").strip()
            checked_at = (row.get("fetched_at") or "").strip()
            if not repo or not sha:
                continue
            metrics = {col: _parse_value(row.get(col, "")) for col in metric_cols}
            upsert_snapshot(
                dest_path,
                repo=repo,
                repo_id=repo_ids.get(repo, ""),
                commit_sha=sha,
                metrics=metrics,
                checked_at=checked_at,
            )
            pairs.add((repo, sha))
    return rows_read, pairs


def reconstruct(rows: dict, repo: str) -> dict[str, dict[str, str]]:
    """Group long rows for `repo` by sha → {metric: value}."""
    out: dict[str, dict[str, str]] = {}
    for (r, sha, metric), row in rows.items():
        if r != repo:
            continue
        out.setdefault(sha, {})[metric] = row["value"]
    return out


def main() -> int:
    console = Console()
    console.rule("[bold]migrate_lizard[/]")
    console.print(f"src cognitive       : {COGNITIVE_CSV}")
    console.print(f"src cyclo-halstead  : {CYCLO_CSV}")
    console.print(f"eligibility (ids)   : {ELIGIBILITY_CSV}")
    console.print(f"destination         : {DEST_CSV}\n")

    if DEST_CSV.exists():
        console.print(f"[red]ABORT[/] destination already exists: {DEST_CSV}")
        console.print("Refusing to overwrite. Move/delete it manually if you really want to redo.")
        return 1

    repo_ids = load_repo_ids(ELIGIBILITY_CSV)
    console.print(f"loaded {len(repo_ids)} repo→repo_id mappings from eligibility\n")

    # 1) cognitive first → 2) cyclo-halstead second (so its `files` wins on tie).
    cog_read, cog_pairs = migrate_source(
        COGNITIVE_CSV, COGNITIVE_METRICS, repo_ids, DEST_CSV
    )
    cyc_read, cyc_pairs = migrate_source(
        CYCLO_CSV, CYCLO_METRICS, repo_ids, DEST_CSV
    )

    # Validate — read back the destination.
    rows = read_long(DEST_CSV)
    written = len(rows)
    overlap = cog_pairs & cyc_pairs

    # Expected: each unique (repo, sha) pair from cognitive contributes
    # len(COGNITIVE_METRICS) rows; same for cyclo-halstead. Overlapping
    # pairs share one `files` metric — the second pass upserts (overwrites)
    # rather than appends, so we subtract one row per overlap pair.
    # Source rows with an empty `analyzed_sha` are correctly dropped (we
    # can't pin metrics to a missing commit), so they don't count.
    expected = (
        len(cog_pairs) * len(COGNITIVE_METRICS)
        + len(cyc_pairs) * len(CYCLO_METRICS)
        - len(overlap)
    )

    # Spot-check 3 known repos: prefer ones in the overlap if available,
    # else fall back to cyclo-only and cognitive-only samples.
    sample_repos: list[str] = []
    if overlap:
        sample_repos.append(next(iter(overlap))[0])
    cyc_only = [p for p in cyc_pairs if p not in overlap]
    if cyc_only:
        sample_repos.append(cyc_only[0][0])
    cog_only = [p for p in cog_pairs if p not in overlap]
    if cog_only:
        sample_repos.append(cog_only[0][0])
    # Top up to 3 from cognitive if needed.
    for (r, _) in sorted(cog_pairs):
        if len(sample_repos) >= 3:
            break
        if r not in sample_repos:
            sample_repos.append(r)

    console.print()
    console.rule("[bold]spot-checks[/]")
    for repo in sample_repos[:3]:
        recon = reconstruct(rows, repo)
        console.print(f"\n[cyan]{repo}[/]")
        for sha, m in recon.items():
            console.print(f"  sha={sha[:10]}…  {m}")

    # Stats table.
    console.print()
    table = Table(title="migration stats", show_lines=False)
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    cog_skipped = cog_read - len(cog_pairs)
    cyc_skipped = cyc_read - len(cyc_pairs)
    table.add_row("cognitive rows read", str(cog_read))
    table.add_row("cognitive rows skipped (empty sha)", str(cog_skipped))
    table.add_row("cyclo-halstead rows read", str(cyc_read))
    table.add_row("cyclo-halstead rows skipped", str(cyc_skipped))
    table.add_row("source rows total", str(cog_read + cyc_read))
    table.add_row("cognitive (repo,sha) pairs", str(len(cog_pairs)))
    table.add_row("cyclo-halstead (repo,sha) pairs", str(len(cyc_pairs)))
    table.add_row("overlapping (repo,sha) pairs", str(len(overlap)))
    table.add_row("unique (repo,sha) pairs", str(len(cog_pairs | cyc_pairs)))
    table.add_row("expected long rows", str(expected))
    table.add_row("[bold]written long rows[/]", f"[bold]{written}[/]")
    table.add_row("match?", "[green]yes[/]" if written == expected else "[red]NO[/]")
    console.print(table)

    # First 5 rows of the new CSV.
    console.print()
    console.rule("[bold]sample (first 5 rows of data/git/lizard.csv)[/]")
    with DEST_CSV.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            console.print(line.rstrip())
            if i >= 5:
                break

    if written != expected:
        console.print(
            f"\n[red]row count mismatch[/]: expected {expected}, got {written}"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
