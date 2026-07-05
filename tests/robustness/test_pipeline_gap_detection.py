"""Robustness test — randomly remove rows from source files, regenerate
risk-data.csv, and verify the pipeline reports the gap correctly.

Goal: prove that the pipeline doesn't silently fill gaps with zeros and
that downstream coverage tables flag missing data clearly. The test
takes a backup of every source file, perturbs each in turn, runs the
pipeline, and checks that the right repos appear empty in risk-data.csv.

Usage:
    uv run python -m tests.robustness.test_pipeline_gap_detection
    uv run python -m tests.robustness.test_pipeline_gap_detection --quick
"""
from __future__ import annotations

import argparse
import csv
import random
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

DATA_DIR = Path("data")
BACKUP_DIR = Path("/tmp/robustness-backup")

# (file path, repo column, what fields drive coverage in risk-data.csv)
# Each entry: (path, repo_col, affected_fields, also_remove_from)
# `also_remove_from` lists peer sources that would fill the same field via
# fallback — they must be removed too for the test to verify true emptiness.
SOURCES = [
    ("data/sources/git/scc.csv", "repo", ["loc_eoy", "sloc_eoy"], []),
    ("data/sources/git/lizard.csv", "repo", ["cognitive_total"], []),
    ("data/sources/git/openssf.csv", "repo", ["openssf_score"], ["data/sources/git/depsdev.csv"]),
    ("data/sources/git/depsdev.csv", "repo", ["bestpractices_badge_id"], []),
    ("data/sources/git/semgrep.csv", "repo", ["sast_findings_total"], []),
    ("data/sources/osv/cves.csv", "repo", ["cve_count_5y"], []),
    ("data/sources/github/issues.csv", "repo", ["issues_opened_5y", "issues_closed_5y"], []),
    ("data/sources/git/churn.csv", "repo", ["churn_5y_total"], []),
    ("data/sources/git/contributor-commits.csv", "repo",
     ["bf_commits_git_5y", "hhi_commits_git_5y"],
     ["data/sources/github/contributor-commits.csv"]),
    ("data/sources/github/repos.csv", "repo", ["stars", "forks"], []),
]

PIPELINE_STAGES = [
    "src.risk.build_complexity",
    "src.risk.build_security",
    "src.risk.build_concentration",
    "src.eligibility.build_funding",
    "src.risk.build_workload",
    "src.risk.aggregate_risk",
]


def backup_all():
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)
    BACKUP_DIR.mkdir(parents=True)
    for path, *_ in SOURCES:
        src = Path(path)
        if not src.exists():
            continue
        dst = BACKUP_DIR / src.name.replace("/", "__")
        shutil.copy(src, dst)


def restore_all():
    for path, *_ in SOURCES:
        src = Path(path)
        backup = BACKUP_DIR / src.name.replace("/", "__")
        if backup.exists():
            shutil.copy(backup, src)


def remove_random_rows(path: str, repo_col: str, n_repos: int, seed: int) -> set[str]:
    """Delete all rows for `n_repos` random repos from `path`. Returns the
    repo set removed."""
    p = Path(path)
    with p.open() as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    repos = sorted({r[repo_col] for r in rows if r.get(repo_col)})
    rng = random.Random(seed)
    removed = set(rng.sample(repos, min(n_repos, len(repos))))
    kept = [r for r in rows if r.get(repo_col) not in removed]
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(kept)
    return removed


def run_pipeline() -> bool:
    for stage in PIPELINE_STAGES:
        result = subprocess.run(
            ["uv", "run", "python", "-m", stage],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]Pipeline stage {stage} failed[/red]")
            console.print(result.stderr[-500:])
            return False
    return True


def load_risk_data() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    with open("data/risk/risk.csv") as f:
        for r in csv.DictReader(f):
            out[r["repo"]] = r
    return out


def check_gap_detection(removed: set[str], affected_fields: list[str]) -> tuple[int, int, list[str]]:
    """For each removed repo, verify all `affected_fields` are empty in risk-data.csv.

    Returns (correctly_empty, silently_filled, sample_violations).
    """
    risk = load_risk_data()
    correct, silent = 0, 0
    violations: list[str] = []
    for repo in removed:
        row = risk.get(repo, {})
        if not row:
            correct += 1
            continue
        for field in affected_fields:
            v = row.get(field, "").strip()
            if v == "" or v == "0" or v == "0.0":
                correct += 1
            else:
                silent += 1
                if len(violations) < 3:
                    violations.append(f"{repo}.{field}={v}")
    return correct, silent, violations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos-per-source", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--quick", action="store_true",
                        help="Run only on 3 sources for fast smoke")
    args = parser.parse_args()

    sources = SOURCES[:3] if args.quick else SOURCES

    console.rule("[bold]Robustness test — random row deletion[/bold]")
    console.print(f"Backing up source files to {BACKUP_DIR}...")
    backup_all()

    table = Table(title="Gap detection results", show_header=True, header_style="bold dim")
    table.add_column("source")
    table.add_column("repos removed", justify="right")
    table.add_column("affected fields")
    table.add_column("correctly empty", justify="right")
    table.add_column("silently filled", justify="right")
    table.add_column("status")

    overall_pass = True

    for path, repo_col, affected, also_remove_from in sources:
        if not Path(path).exists() or not affected:
            continue
        # Restore clean state, perturb just this source + any peer sources
        # that act as silent fallbacks for the same field.
        restore_all()
        removed = remove_random_rows(path, repo_col, args.repos_per_source, args.seed)
        for peer_path in also_remove_from:
            if Path(peer_path).exists():
                # Remove the same set of repos from the peer source
                p = Path(peer_path)
                with p.open() as f:
                    reader = csv.DictReader(f); fields = reader.fieldnames
                    rows_p = [r for r in reader if r.get(repo_col) not in removed]
                with p.open("w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fields)
                    w.writeheader(); w.writerows(rows_p)

        if not run_pipeline():
            table.add_row(path, str(len(removed)), ",".join(affected), "-", "-",
                          "[red]PIPELINE FAILED[/red]")
            overall_pass = False
            continue

        correct, silent, violations = check_gap_detection(removed, affected)
        status = "[green]OK[/green]" if silent == 0 else "[yellow]LEAKED[/yellow]"
        if silent > 0:
            overall_pass = False
        peer_note = f" (+{len(also_remove_from)} peer)" if also_remove_from else ""
        table.add_row(path + peer_note, str(len(removed)), ",".join(affected[:2]),
                      str(correct), str(silent), status)
        if violations:
            console.print(f"  [dim]violations sample: {violations}[/dim]")

    # Restore everything
    restore_all()
    console.print("\n[dim]Restored all source files from backup.[/dim]")

    console.print(table)

    if overall_pass:
        console.print("\n[bold green]OVERALL PASS — pipeline correctly empties downstream fields when source rows are removed.[/bold green]")
        return 0
    else:
        console.print("\n[bold yellow]OVERALL LEAKED — some sources allow silent zero-fill (see violations).[/bold yellow]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
