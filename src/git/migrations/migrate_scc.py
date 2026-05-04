"""One-shot migration: 6 wide-format scc files → data/git/scc.csv (long format).

Reads six wide CSVs (one column per year 2021..2025) and rewrites them in
long format (`repo, repo_id, commit_sha, metric, value, checked_at`) using
`src.git.long_format.upsert_rows`.

Each (repo, year) cell across the 6 sources is pinned to the `last_sha`
recorded for that (repo, year) in `commits-years.csv`. The cell is
skipped if `last_sha` is empty (we can't pin to a commit) or the cell
itself is blank. A measured value of 0 IS preserved.

Source files (read-only):
  - data/github/git/loc.csv             → metric `loc`
  - data/github/git/sloc.csv            → metric `sloc`
  - data/github/git/files.csv           → metric `files`
  - data/github/git/uloc.csv            → metric `uloc`
  - data/github/git/scc-complexity.csv  → metric `complexity`
  - data/github/git/scc-density.csv     → metric `complexity_density`

Lookup tables:
  - data/github/git/commits-years.csv   → (repo, year) → last_sha + fetched_at
  - data/eligibility-data.csv           → repo → repo_id (best-effort, may be empty)

Destination:
  - data/git/scc.csv (created fresh; aborts if it already exists)

Run:
  uv run python -m src.git.migrations.migrate_scc
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
from rich.console import Console
from rich.table import Table

from src.git.long_format import read as read_long
from src.git.long_format import upsert_rows

ROOT = Path(__file__).resolve().parents[3]
GIT_DIR = ROOT / "data" / "github" / "git"
ELIGIBILITY_CSV = ROOT / "data" / "eligibility-data.csv"
COMMITS_YEARS_CSV = GIT_DIR / "commits-years.csv"
DEST_CSV = ROOT / "data" / "git" / "scc.csv"

YEARS = ["2021", "2022", "2023", "2024", "2025"]

# (filename, metric_name)
SOURCES: list[tuple[str, str]] = [
    ("loc.csv", "loc"),
    ("sloc.csv", "sloc"),
    ("files.csv", "files"),
    ("uloc.csv", "uloc"),
    ("scc-complexity.csv", "complexity"),
    ("scc-density.csv", "complexity_density"),
]


def load_repo_ids(path: Path) -> dict[str, str]:
    """Map `repo` → `repo_id` from eligibility-data.csv. Empty string if missing."""
    df = pl.read_csv(path, columns=["repo", "repo_id"], infer_schema_length=0)
    out: dict[str, str] = {}
    for repo, repo_id in zip(
        df["repo"].to_list(), df["repo_id"].to_list(), strict=True
    ):
        repo = (repo or "").strip()
        repo_id = (repo_id or "").strip()
        if repo:
            out[repo] = repo_id
    return out


def load_commits_years(path: Path) -> pl.DataFrame:
    """Load (repo, year, last_sha, fetched_at). Year stays as str for joining."""
    df = pl.read_csv(
        path,
        columns=["repo", "year", "last_sha", "fetched_at"],
        infer_schema_length=0,
    )
    return df.with_columns(
        pl.col("repo").str.strip_chars(),
        pl.col("year").str.strip_chars(),
        pl.col("last_sha").fill_null("").str.strip_chars(),
        pl.col("fetched_at").fill_null("").str.strip_chars(),
    )


def melt_source(path: Path, metric: str) -> pl.DataFrame:
    """Read a wide source CSV and return long (repo, year, metric, value)."""
    df = pl.read_csv(path, infer_schema_length=0)
    long = df.unpivot(
        index="repo",
        on=YEARS,
        variable_name="year",
        value_name="value",
    )
    return long.with_columns(
        pl.col("repo").str.strip_chars(),
        pl.col("year").str.strip_chars(),
        pl.col("value").fill_null("").str.strip_chars(),
        pl.lit(metric).alias("metric"),
    )


def main() -> int:
    console = Console()
    console.rule("[bold]migrate_scc[/]")
    console.print(f"src dir         : {GIT_DIR}")
    console.print(f"commits-years   : {COMMITS_YEARS_CSV}")
    console.print(f"eligibility     : {ELIGIBILITY_CSV}")
    console.print(f"destination     : {DEST_CSV}\n")

    if DEST_CSV.exists():
        console.print(f"[red]ABORT[/] destination already exists: {DEST_CSV}")
        console.print(
            "Refusing to overwrite. Move/delete it manually if you really want to redo."
        )
        return 1

    repo_ids = load_repo_ids(ELIGIBILITY_CSV)
    console.print(f"loaded {len(repo_ids)} repo→repo_id mappings from eligibility")

    commits = load_commits_years(COMMITS_YEARS_CSV)
    console.print(f"loaded {commits.height} (repo, year) rows from commits-years\n")

    # Read & melt each source, count input rows, then concat.
    per_source_input: list[tuple[str, int, int]] = []  # (file, wide_rows, long_cells)
    long_frames: list[pl.DataFrame] = []
    for filename, metric in SOURCES:
        path = GIT_DIR / filename
        wide_df = pl.read_csv(path, infer_schema_length=0)
        long_df = melt_source(path, metric)
        per_source_input.append((filename, wide_df.height, long_df.height))
        long_frames.append(long_df)

    all_long = pl.concat(long_frames)
    total_cells = all_long.height
    # Drop blank cells.
    non_blank = all_long.filter(pl.col("value") != "")
    blank_cells = total_cells - non_blank.height

    # Join with commits-years on (repo, year) → bring in last_sha + fetched_at.
    joined = non_blank.join(commits, on=["repo", "year"], how="left").with_columns(
        pl.col("last_sha").fill_null(""),
        pl.col("fetched_at").fill_null(""),
    )

    # Skip rows where last_sha is empty (can't pin to a commit).
    has_sha = joined.filter(pl.col("last_sha") != "")
    skipped_no_sha = joined.height - has_sha.height

    # Build the output rows: repo, repo_id, commit_sha, metric, value, checked_at.
    repo_id_df = pl.DataFrame(
        {
            "repo": list(repo_ids.keys()),
            "repo_id": list(repo_ids.values()),
        }
    )
    final = (
        has_sha.join(repo_id_df, on="repo", how="left")
        .with_columns(pl.col("repo_id").fill_null(""))
        .select(
            pl.col("repo"),
            pl.col("repo_id"),
            pl.col("last_sha").alias("commit_sha"),
            pl.col("metric"),
            pl.col("value"),
            pl.col("fetched_at").alias("checked_at"),
        )
    )

    rows_to_write = final.to_dicts()
    console.print(f"prepared {len(rows_to_write):,} rows to upsert\n")

    # Bulk upsert (single pass — destination is empty so it's effectively a write).
    written = upsert_rows(DEST_CSV, rows_to_write)

    # Read back for stats / spot-checks.
    rows_back = read_long(DEST_CSV)
    unique_repo_sha = {(r, s) for (r, s, _m) in rows_back.keys()}

    # Stats tables.
    console.print()
    inp = Table(title="input rows per source", show_lines=False)
    inp.add_column("file", style="dim")
    inp.add_column("wide rows", justify="right")
    inp.add_column("cells (rows × 5 yrs)", justify="right")
    total_wide = 0
    total_cells_print = 0
    for filename, wide_rows, long_cells in per_source_input:
        inp.add_row(filename, f"{wide_rows:,}", f"{long_cells:,}")
        total_wide += wide_rows
        total_cells_print += long_cells
    inp.add_row(
        "[bold]TOTAL[/]",
        f"[bold]{total_wide:,}[/]",
        f"[bold]{total_cells_print:,}[/]",
    )
    console.print(inp)

    console.print()
    out = Table(title="migration output", show_lines=False)
    out.add_column("metric", style="dim")
    out.add_column("value", justify="right")
    out.add_row("total cells (all sources × 5 yrs)", f"{total_cells:,}")
    out.add_row("cells blank in source", f"{blank_cells:,}")
    out.add_row("cells skipped (no last_sha)", f"{skipped_no_sha:,}")
    out.add_row("rows prepared", f"{len(rows_to_write):,}")
    out.add_row("[bold]rows written to scc.csv[/]", f"[bold]{written:,}[/]")
    out.add_row("rows read back", f"{len(rows_back):,}")
    out.add_row("unique (repo, sha) pairs", f"{len(unique_repo_sha):,}")
    console.print(out)

    # Validation: pick 3 (repo, year) cells where last_sha is non-empty AND
    # at least one source cell was populated, then verify metrics are present.
    console.print()
    console.rule("[bold]validation: 3 (repo, year) → 6 metrics[/]")
    sample = (
        has_sha.group_by(["repo", "year", "last_sha"])
        .agg(pl.len().alias("n_metrics_with_data"))
        .filter(pl.col("n_metrics_with_data") == len(SOURCES))
        .head(3)
        .select(["repo", "year", "last_sha"])
        .to_dicts()
    )
    metric_names = {m for _, m in SOURCES}
    for s in sample:
        repo = s["repo"]
        year = s["year"]
        sha = s["last_sha"]
        present = {
            metric: row["value"]
            for (r, sh, metric), row in rows_back.items()
            if r == repo and sh == sha
        }
        missing = metric_names - set(present.keys())
        console.print(
            f"\n[cyan]{repo}[/]  year={year}  sha={sha[:10]}…"
        )
        for m in sorted(present):
            console.print(f"  {m:20s} = {present[m]}")
        if missing:
            # Could be due to blank source cells — note them.
            console.print(
                f"  [yellow]missing metrics (likely blank source cells)[/]: {sorted(missing)}"
            )

    # Sample 5 rows of the destination CSV.
    console.print()
    console.rule("[bold]sample (first 5 rows of data/git/scc.csv)[/]")
    with DEST_CSV.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            console.print(line.rstrip())
            if i >= 5:
                break

    return 0


if __name__ == "__main__":
    sys.exit(main())
