"""Validate Phase 2: round-trip the 4 migrated long-format files against
their source data and report any UNEXPECTED differences.

For each source we reconstruct the source-shaped data from the long
file and diff against the original. The "expected" diffs are documented
per-source below — anything else is flagged as an anomaly and the
script exits non-zero.

Sources validated:
  - data/sources/git/lizard.csv  ← cognitive.csv + cyclo-halstead.csv
  - data/sources/git/semgrep.csv ← github/git/semgrep.csv
  - data/sources/git/scc.csv     ← 6 wide files + commits-years.csv (sha lookup)
  - data/sources/git/openssf.csv ← openssf/data.json

Run:
  uv run python -m src.sources.git.migrations.validate
"""
from __future__ import annotations

import csv
import json
import random
import re
import sys
from pathlib import Path

import polars as pl
from rich.console import Console
from rich.table import Table

from src.sources.git.long_format import _format_value
from src.sources.git.long_format import read as read_long

ROOT = Path(__file__).resolve().parents[4]

# Source paths.
COGNITIVE_CSV = ROOT / "data" / "sources" / "github" / "git" / "cognitive.csv"
CYCLO_CSV = ROOT / "data" / "sources" / "github" / "git" / "cyclo-halstead.csv"
SEMGREP_SRC = ROOT / "data" / "sources" / "github" / "git" / "semgrep.csv"
SCC_DIR = ROOT / "data" / "sources" / "github" / "git"
COMMITS_YEARS_CSV = ROOT / "data" / "sources" / "github" / "git" / "commits-years.csv"
OPENSSF_JSON = ROOT / "data" / "sources" / "openssf" / "data.json"

# Long-format destinations.
LIZARD_LONG = ROOT / "data" / "sources" / "git" / "lizard.csv"
SEMGREP_LONG = ROOT / "data" / "sources" / "git" / "semgrep.csv"
SCC_LONG = ROOT / "data" / "sources" / "git" / "scc.csv"
OPENSSF_LONG = ROOT / "data" / "sources" / "git" / "openssf.csv"

YEARS = ["2021", "2022", "2023", "2024", "2025"]
SCC_SOURCES: list[tuple[str, str]] = [
    ("loc.csv", "loc"),
    ("sloc.csv", "sloc"),
    ("files.csv", "files"),
    ("uloc.csv", "uloc"),
    ("scc-complexity.csv", "complexity"),
    ("scc-density.csv", "complexity_density"),
]

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
SEMGREP_NUMERIC: tuple[str, ...] = (
    "findings_total",
    "findings_error",
    "findings_warning",
    "findings_info",
    "findings_security",
    "findings_correctness",
    "findings_performance",
    "top_rule_count",
)


def _parse_value(raw: str):
    """Best-effort int/float/str parse. Empty → None."""
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None
    try:
        if "." not in s and "e" not in s and "E" not in s:
            return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _expected_str(value) -> str:
    """Mirror the long-format `_format_value` so we can compare against
    the stored CSV string without re-parsing it."""
    return _format_value(value)


def _rulepack_prefix(rulepack: str) -> str:
    return rulepack.replace("/", "_").replace("-", "_").lower()


def _to_snake_case(name: str) -> str:
    s = name.strip().replace("-", "_")
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s.lower()


# -----------------------------------------------------------------------
# lizard
# -----------------------------------------------------------------------
def validate_lizard(long_rows: dict, console: Console) -> dict:
    """Round-trip cognitive + cyclo-halstead vs lizard.csv.

    Expected drops: 82 cognitive rows with empty analyzed_sha. Where both
    sources cover the same (repo, sha), cyclo-halstead's `files` value
    wins (later fetched_at)."""
    anomalies: list[str] = []

    # Read cognitive source.
    cog_rows: list[dict] = []
    cog_dropped_empty_sha = 0
    with COGNITIVE_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sha = (row.get("analyzed_sha") or "").strip()
            if not sha:
                cog_dropped_empty_sha += 1
                continue
            cog_rows.append(row)
    cog_src_total = cog_dropped_empty_sha + len(cog_rows)

    # Read cyclo-halstead source.
    cyc_rows: list[dict] = []
    with CYCLO_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sha = (row.get("analyzed_sha") or "").strip()
            if not sha:
                anomalies.append(f"cyclo-halstead row with empty sha: {row.get('repo')}")
                continue
            cyc_rows.append(row)
    cyc_src_total = len(cyc_rows)

    # Index lizard.csv per (repo, sha) → {metric: value}.
    by_pair: dict[tuple[str, str], dict[str, str]] = {}
    for (repo, sha, metric), row in long_rows.items():
        by_pair.setdefault((repo, sha), {})[metric] = row["value"]

    # 1) Each cognitive (repo, sha) has the cognitive_* metrics.
    cog_pairs: set[tuple[str, str]] = set()
    cog_mismatches: list[str] = []
    for row in cog_rows:
        repo = row["repo"].strip()
        sha = row["analyzed_sha"].strip()
        cog_pairs.add((repo, sha))
        present = by_pair.get((repo, sha), {})
        for metric in ["cognitive_total", "cognitive_avg", "cognitive_max"]:
            want = _expected_str(_parse_value(row.get(metric, "")))
            if want == "":
                continue
            got = present.get(metric)
            if got != want:
                cog_mismatches.append(
                    f"{repo}@{sha[:7]} {metric}: want={want!r} got={got!r}"
                )

    # 2) Each cyclo (repo, sha) has the cyclomatic_*/halstead_*/MI/files metrics.
    cyc_pairs: set[tuple[str, str]] = set()
    cyc_mismatches: list[str] = []
    for row in cyc_rows:
        repo = row["repo"].strip()
        sha = row["analyzed_sha"].strip()
        cyc_pairs.add((repo, sha))
        present = by_pair.get((repo, sha), {})
        for metric in CYCLO_METRICS:
            want = _expected_str(_parse_value(row.get(metric, "")))
            if want == "":
                continue
            got = present.get(metric)
            if got != want:
                cyc_mismatches.append(
                    f"{repo}@{sha[:7]} {metric}: want={want!r} got={got!r}"
                )

    # 3) For overlapping (repo, sha): cyclo's `files` should be in long file.
    overlap = cog_pairs & cyc_pairs
    files_winner_violations: list[str] = []
    for repo, sha in overlap:
        cyc_row = next(
            (r for r in cyc_rows if r["repo"].strip() == repo and r["analyzed_sha"].strip() == sha),
            None,
        )
        if cyc_row is None:
            continue
        want = _expected_str(_parse_value(cyc_row.get("files", "")))
        got = by_pair.get((repo, sha), {}).get("files")
        if want != "" and got != want:
            files_winner_violations.append(
                f"{repo}@{sha[:7]} files: want(cyclo)={want!r} got={got!r}"
            )

    # 4) Confirm 82 dropped rows had empty analyzed_sha.
    dropped_check_pass = cog_dropped_empty_sha == 82

    # Expected long row count: each cog pair contributes COGNITIVE_METRICS
    # rows; each cyc pair contributes CYCLO_METRICS rows; overlap shares
    # the `files` metric (one row), so subtract len(overlap).
    # Empty values get dropped by the long format too — count them.
    def _count_nonempty(rows, metrics):
        n = 0
        for r in rows:
            for m in metrics:
                v = _expected_str(_parse_value(r.get(m, "")))
                if v != "":
                    n += 1
        return n

    cog_nonempty = _count_nonempty(cog_rows, COGNITIVE_METRICS)
    cyc_nonempty = _count_nonempty(cyc_rows, CYCLO_METRICS)
    # If a pair is in overlap, cyc.files overwrites cog.files — so we
    # subtract the cog-files contribution for overlapping rows where the
    # cyc-files value is itself non-empty (otherwise the cog one wins).
    overwrite_drops = 0
    for repo, sha in overlap:
        cog_row = next(
            (r for r in cog_rows if r["repo"].strip() == repo and r["analyzed_sha"].strip() == sha),
            None,
        )
        cyc_row = next(
            (r for r in cyc_rows if r["repo"].strip() == repo and r["analyzed_sha"].strip() == sha),
            None,
        )
        if cog_row is None or cyc_row is None:
            continue
        cog_v = _expected_str(_parse_value(cog_row.get("files", "")))
        cyc_v = _expected_str(_parse_value(cyc_row.get("files", "")))
        if cog_v != "" and cyc_v != "":
            overwrite_drops += 1
    expected_long_rows = cog_nonempty + cyc_nonempty - overwrite_drops

    long_rows_count = len(long_rows)

    # Anomalies: mismatches and unexpected counts.
    if cog_mismatches:
        anomalies.append(f"{len(cog_mismatches)} cognitive metric mismatches (showing 5):")
        anomalies.extend([f"  - {m}" for m in cog_mismatches[:5]])
    if cyc_mismatches:
        anomalies.append(f"{len(cyc_mismatches)} cyclo-halstead metric mismatches (showing 5):")
        anomalies.extend([f"  - {m}" for m in cyc_mismatches[:5]])
    if files_winner_violations:
        anomalies.append(
            f"{len(files_winner_violations)} files-overwrite violations (cyclo should win):"
        )
        anomalies.extend([f"  - {m}" for m in files_winner_violations[:5]])
    if not dropped_check_pass:
        anomalies.append(
            f"expected 82 cognitive rows dropped for empty sha, got {cog_dropped_empty_sha}"
        )
    if long_rows_count != expected_long_rows:
        anomalies.append(
            f"long row count mismatch: expected {expected_long_rows}, got {long_rows_count}"
        )

    status = "PASS" if not anomalies else "FAIL"

    console.print(f"\n[bold]lizard[/]")
    console.print(f"  cognitive rows in source       : {cog_src_total}")
    console.print(f"    dropped (empty analyzed_sha) : {cog_dropped_empty_sha}")
    console.print(f"    kept (used for migration)    : {len(cog_rows)}")
    console.print(f"  cyclo-halstead rows in source  : {cyc_src_total}")
    console.print(f"  unique (repo,sha) pairs cog    : {len(cog_pairs)}")
    console.print(f"  unique (repo,sha) pairs cyc    : {len(cyc_pairs)}")
    console.print(f"  overlapping pairs              : {len(overlap)}")
    console.print(f"  expected long rows             : {expected_long_rows}")
    console.print(f"  actual long rows               : {long_rows_count}")
    console.print(f"  cognitive metric mismatches    : {len(cog_mismatches)}")
    console.print(f"  cyclo-halstead metric mismatches: {len(cyc_mismatches)}")
    console.print(f"  files-winner violations        : {len(files_winner_violations)}")

    return {
        "source_rows": cog_src_total + cyc_src_total,
        "long_rows": long_rows_count,
        "expected_drops": 82,
        "actual_drops": cog_dropped_empty_sha,
        "status": status,
        "anomalies": anomalies,
    }


# -----------------------------------------------------------------------
# semgrep
# -----------------------------------------------------------------------
def validate_semgrep(long_rows: dict, console: Console) -> dict:
    """Round-trip semgrep — every numeric metric should match exactly,
    under prefix `<rulepack_snake>.<metric>`. Source rows with empty
    findings_total are skipped (timeouts)."""
    anomalies: list[str] = []
    src_rows = 0
    skipped_no_total = 0
    expected_pairs: list[tuple[str, str, str, str]] = []  # (repo,sha,metric,want)

    with SEMGREP_SRC.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            src_rows += 1
            repo = (row.get("repo") or "").strip()
            sha = (row.get("analyzed_sha") or "").strip()
            rulepack = (row.get("rulepack") or "").strip()
            if (row.get("findings_total") or "").strip() == "":
                skipped_no_total += 1
                continue
            if not repo or not sha or not rulepack:
                skipped_no_total += 1
                continue
            prefix = _rulepack_prefix(rulepack)
            for col in SEMGREP_NUMERIC:
                parsed = _parse_value(row.get(col, ""))
                if parsed is None:
                    continue
                want = _expected_str(parsed)
                if want == "":
                    continue
                expected_pairs.append((repo, sha, f"{prefix}.{col}", want))

    mismatches: list[str] = []
    missing: list[str] = []
    for repo, sha, metric, want in expected_pairs:
        got_row = long_rows.get((repo, sha, metric))
        if got_row is None:
            missing.append(f"{repo}@{sha[:7]} {metric} (want={want!r})")
            continue
        if got_row["value"] != want:
            mismatches.append(
                f"{repo}@{sha[:7]} {metric}: want={want!r} got={got_row['value']!r}"
            )

    expected_long_rows = len(expected_pairs)
    long_rows_count = len(long_rows)

    if missing:
        anomalies.append(f"{len(missing)} expected rows missing from long file (showing 5):")
        anomalies.extend([f"  - {m}" for m in missing[:5]])
    if mismatches:
        anomalies.append(f"{len(mismatches)} value mismatches (showing 5):")
        anomalies.extend([f"  - {m}" for m in mismatches[:5]])
    if long_rows_count != expected_long_rows:
        anomalies.append(
            f"row count mismatch: expected {expected_long_rows}, got {long_rows_count}"
        )

    status = "PASS" if not anomalies else "FAIL"

    console.print(f"\n[bold]semgrep[/]")
    console.print(f"  source rows                     : {src_rows}")
    console.print(f"  source rows skipped (no total)  : {skipped_no_total}")
    console.print(f"  expected long rows              : {expected_long_rows}")
    console.print(f"  actual long rows                : {long_rows_count}")
    console.print(f"  missing rows                    : {len(missing)}")
    console.print(f"  value mismatches                : {len(mismatches)}")

    return {
        "source_rows": src_rows,
        "long_rows": long_rows_count,
        "expected_drops": skipped_no_total,
        "actual_drops": skipped_no_total,
        "status": status,
        "anomalies": anomalies,
    }


# -----------------------------------------------------------------------
# scc
# -----------------------------------------------------------------------
def validate_scc(long_rows: dict, console: Console) -> dict:
    """Round-trip 6 wide scc files vs scc.csv.

    Round-trip should hold ONLY for (repo, year) cells where
    commits-years.csv has a non-empty last_sha. Sample 100 such cells
    (with non-blank source values) and verify all 6 metrics are present
    with exact source values."""
    anomalies: list[str] = []

    # Load commits-years to map (repo, year) → last_sha.
    cy = pl.read_csv(
        COMMITS_YEARS_CSV,
        columns=["repo", "year", "last_sha"],
        infer_schema_length=0,
    ).with_columns(
        pl.col("repo").str.strip_chars(),
        pl.col("year").str.strip_chars(),
        pl.col("last_sha").fill_null("").str.strip_chars(),
    )
    sha_lookup: dict[tuple[str, str], str] = {
        (r, y): s
        for r, y, s in zip(
            cy["repo"].to_list(), cy["year"].to_list(), cy["last_sha"].to_list(),
            strict=True,
        )
    }

    # Read each source, melt to long, classify each cell.
    total_cells = 0
    blank_cells = 0
    no_sha_drops = 0
    valid_cells: list[dict] = []  # rows with non-blank value AND non-empty last_sha

    for filename, metric in SCC_SOURCES:
        path = SCC_DIR / filename
        df = pl.read_csv(path, infer_schema_length=0)
        long_df = df.unpivot(
            index="repo", on=YEARS, variable_name="year", value_name="value"
        ).with_columns(
            pl.col("repo").str.strip_chars(),
            pl.col("year").str.strip_chars(),
            pl.col("value").fill_null("").str.strip_chars(),
        )
        for repo, year, value in zip(
            long_df["repo"].to_list(),
            long_df["year"].to_list(),
            long_df["value"].to_list(),
            strict=True,
        ):
            total_cells += 1
            if value == "":
                blank_cells += 1
                continue
            sha = sha_lookup.get((repo, year), "")
            if not sha:
                no_sha_drops += 1
                continue
            valid_cells.append(
                {"repo": repo, "year": year, "metric": metric, "value": value, "sha": sha}
            )

    # Each valid cell should produce exactly one row in the long file.
    expected_long_rows = len(valid_cells)
    long_rows_count = len(long_rows)

    # Sample 100 random cells and verify exact match.
    rnd = random.Random(42)
    sample_size = min(100, len(valid_cells))
    sample = rnd.sample(valid_cells, sample_size) if valid_cells else []
    sampled_mismatches: list[str] = []
    sampled_missing: list[str] = []
    for c in sample:
        key = (c["repo"], c["sha"], c["metric"])
        got = long_rows.get(key)
        if got is None:
            sampled_missing.append(
                f"{c['repo']}@{c['sha'][:7]} year={c['year']} metric={c['metric']} (value={c['value']!r})"
            )
            continue
        if got["value"] != c["value"]:
            sampled_mismatches.append(
                f"{c['repo']}@{c['sha'][:7]} {c['metric']}: src={c['value']!r} long={got['value']!r}"
            )

    # Validate the documented expected drop counts.
    blank_match = abs(blank_cells - 11052) <= 5  # ~11,052 per spec
    no_sha_match = no_sha_drops == 912462

    if sampled_missing:
        anomalies.append(f"{len(sampled_missing)} sampled cells missing from long file:")
        anomalies.extend([f"  - {m}" for m in sampled_missing[:5]])
    if sampled_mismatches:
        anomalies.append(f"{len(sampled_mismatches)} sampled cells with value mismatch:")
        anomalies.extend([f"  - {m}" for m in sampled_mismatches[:5]])
    if not blank_match:
        anomalies.append(f"blank-cell count {blank_cells} differs from expected ~11,052")
    if not no_sha_match:
        anomalies.append(
            f"no-sha drops count {no_sha_drops} differs from documented 912,462"
        )
    if long_rows_count != expected_long_rows:
        anomalies.append(
            f"long row count mismatch: expected {expected_long_rows}, got {long_rows_count}"
        )

    status = "PASS" if not anomalies else "FAIL"

    console.print(f"\n[bold]scc[/]")
    console.print(f"  total cells (6 sources × 5 yrs) : {total_cells:,}")
    console.print(f"  blank in source                 : {blank_cells:,} (spec ~11,052)")
    console.print(f"  skipped (no last_sha)           : {no_sha_drops:,} (spec 912,462)")
    console.print(f"  valid cells                     : {len(valid_cells):,}")
    console.print(f"  expected long rows              : {expected_long_rows:,}")
    console.print(f"  actual long rows                : {long_rows_count:,}")
    console.print(f"  sampled cells checked           : {sample_size}")
    console.print(f"  sampled missing                 : {len(sampled_missing)}")
    console.print(f"  sampled mismatches              : {len(sampled_mismatches)}")

    return {
        "source_rows": total_cells,
        "long_rows": long_rows_count,
        "expected_drops": blank_cells + no_sha_drops,
        "actual_drops": blank_cells + no_sha_drops,
        "status": status,
        "anomalies": anomalies,
    }


# -----------------------------------------------------------------------
# openssf
# -----------------------------------------------------------------------
def validate_openssf(long_rows: dict, console: Console) -> dict:
    """Round-trip openssf data.json vs openssf.csv. Every JSON entry's
    score and every checks[].score should be in the long file under
    (repo_key, repo.commit, snake_case(name)). -1 must be preserved.
    No entry should be dropped (none had error:true or missing commit)."""
    anomalies: list[str] = []
    with OPENSSF_JSON.open(encoding="utf-8") as f:
        data = json.load(f)

    total_entries = len(data)
    errored = 0
    missing_commit = 0
    expected: list[tuple[str, str, str, str]] = []  # (repo, sha, metric, want)
    minus_one_count = 0

    for repo_key, entry in data.items():
        if not isinstance(entry, dict):
            errored += 1
            continue
        if entry.get("error"):
            errored += 1
            continue
        repo_obj = entry.get("repo") or {}
        sha = (repo_obj.get("commit") or "").strip()
        if not sha:
            missing_commit += 1
            continue

        top_score = entry.get("score")
        if top_score is not None:
            want = _expected_str(top_score)
            if want != "":
                expected.append((repo_key, sha, "score", want))

        for check in entry.get("checks") or []:
            name = (check or {}).get("name")
            score = (check or {}).get("score")
            if not name or score is None:
                continue
            metric = _to_snake_case(name)
            want = _expected_str(score)
            if want == "":
                continue
            if score == -1:
                minus_one_count += 1
            expected.append((repo_key, sha, metric, want))

    missing: list[str] = []
    mismatches: list[str] = []
    for repo, sha, metric, want in expected:
        got = long_rows.get((repo, sha, metric))
        if got is None:
            missing.append(f"{repo}@{sha[:7]} {metric} (want={want!r})")
            continue
        if got["value"] != want:
            mismatches.append(
                f"{repo}@{sha[:7]} {metric}: want={want!r} got={got['value']!r}"
            )

    # Verify -1 preservation by counting them in the long file.
    minus_one_in_long = sum(1 for row in long_rows.values() if row["value"] == "-1")

    expected_long_rows = len(expected)
    long_rows_count = len(long_rows)

    if errored:
        anomalies.append(f"{errored} JSON entries marked errored — spec said zero")
    if missing_commit:
        anomalies.append(f"{missing_commit} JSON entries missing commit — spec said zero")
    if missing:
        anomalies.append(f"{len(missing)} expected rows missing from long file:")
        anomalies.extend([f"  - {m}" for m in missing[:5]])
    if mismatches:
        anomalies.append(f"{len(mismatches)} value mismatches:")
        anomalies.extend([f"  - {m}" for m in mismatches[:5]])
    if minus_one_in_long != minus_one_count:
        anomalies.append(
            f"-1 preservation: source has {minus_one_count}, long has {minus_one_in_long}"
        )
    if long_rows_count != expected_long_rows:
        anomalies.append(
            f"row count mismatch: expected {expected_long_rows}, got {long_rows_count}"
        )

    status = "PASS" if not anomalies else "FAIL"

    console.print(f"\n[bold]openssf[/]")
    console.print(f"  JSON entries                   : {total_entries}")
    console.print(f"  errored entries                : {errored}")
    console.print(f"  entries missing commit         : {missing_commit}")
    console.print(f"  expected long rows             : {expected_long_rows}")
    console.print(f"  actual long rows               : {long_rows_count}")
    console.print(f"  missing rows                   : {len(missing)}")
    console.print(f"  value mismatches               : {len(mismatches)}")
    console.print(f"  -1 values in source            : {minus_one_count}")
    console.print(f"  -1 values in long              : {minus_one_in_long}")

    return {
        "source_rows": total_entries,
        "long_rows": long_rows_count,
        "expected_drops": errored + missing_commit,
        "actual_drops": errored + missing_commit,
        "status": status,
        "anomalies": anomalies,
    }


# -----------------------------------------------------------------------
# main
# -----------------------------------------------------------------------
def main() -> int:
    console = Console()
    console.rule("[bold]validate Phase 2 round-trip[/]")
    console.print(f"root: {ROOT}\n")

    # Load all 4 long files up front.
    console.print("loading long files…")
    lizard = read_long(LIZARD_LONG)
    semgrep = read_long(SEMGREP_LONG)
    scc = read_long(SCC_LONG)
    openssf = read_long(OPENSSF_LONG)
    console.print(
        f"  lizard.csv : {len(lizard):,} rows\n"
        f"  semgrep.csv: {len(semgrep):,} rows\n"
        f"  scc.csv    : {len(scc):,} rows\n"
        f"  openssf.csv: {len(openssf):,} rows"
    )

    results = {}
    results["lizard"] = validate_lizard(lizard, console)
    results["semgrep"] = validate_semgrep(semgrep, console)
    results["scc"] = validate_scc(scc, console)
    results["openssf"] = validate_openssf(openssf, console)

    # Per-source anomaly detail.
    console.print()
    console.rule("[bold]anomalies[/]")
    any_anomalies = False
    for name, r in results.items():
        if r["anomalies"]:
            any_anomalies = True
            console.print(f"\n[red]{name}[/]")
            for a in r["anomalies"]:
                console.print(f"  {a}")
    if not any_anomalies:
        console.print("[green]none[/] — all diffs are expected.")

    # Summary table.
    console.print()
    summary = Table(title="Phase 2 validation summary", show_lines=False)
    summary.add_column("source", style="dim")
    summary.add_column("source rows", justify="right")
    summary.add_column("long rows", justify="right")
    summary.add_column("expected drops", justify="right")
    summary.add_column("actual drops", justify="right")
    summary.add_column("status")
    for name, r in results.items():
        color = "green" if r["status"] == "PASS" else "red"
        summary.add_row(
            name,
            f"{r['source_rows']:,}",
            f"{r['long_rows']:,}",
            f"{r['expected_drops']:,}",
            f"{r['actual_drops']:,}",
            f"[{color}]{r['status']}[/]",
        )
    console.print(summary)

    failed = [n for n, r in results.items() if r["status"] != "PASS"]
    if failed:
        console.print(f"\n[red]FAIL[/]: {', '.join(failed)}")
        return 1
    console.print("\n[green]PASS[/] — all 4 sources round-trip cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
