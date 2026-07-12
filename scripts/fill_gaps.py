"""Auto-fill missing data per source for all eligible repos, then rebuild
category CSVs and risk-data.

Detects gaps by comparing eligible repos against each source file's
unique repo set, then invokes the appropriate fetcher. Each fetcher has
its own smart-upsert / TTL semantics, so re-running is cheap if there
are no gaps.

Usage:
    uv run python scripts/fill_gaps.py
    uv run python scripts/fill_gaps.py --build-only     # just rebuild CSVs
    uv run python scripts/fill_gaps.py --dry-run        # show plan, no changes

Order is important — foundation sources (commits-years, resolve_head) are
filled first so sha-pinned fetchers have the data they need to walk down
to a real snapshot.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common.repos import load_top_repos  # noqa: E402


def _run(cmd: list[str], log_path: Path) -> int:
    """Run a fetcher, tee its output to a log file, return exit code."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    return proc.returncode


def _eligible() -> set[str]:
    """Risk-scope set (value-class A/B, non-archived, valid) — the repos the
    risk fetchers/builders run on."""
    return {e.repo for e in load_top_repos(
        value_file=str(ROOT / "data/value/value.csv"),
        repos_file=str(ROOT / "data/sources/github/repos.csv"),
    ) if e.repo}


def _covered(path: Path, repo_col: str = "repo") -> set[str]:
    if not path.exists():
        return set()
    with path.open() as f:
        return {r[repo_col] for r in csv.DictReader(f) if r.get(repo_col)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    eligible = _eligible()
    console.print(f"[bold]Risk-scope:[/bold] {len(eligible)} repos\n")

    plan: list[tuple[str, list[str], int]] = []  # (label, cmd, missing_count)

    if not args.build_only:
        # 1. Foundation: commits-years for any repos missing
        missing = eligible - _covered(ROOT / "data/sources/git/commits-years.csv")
        if missing:
            plan.append(("commits-years", ["uv", "run", "python", "-m", "src.sources.git.commits_years", "--concurrency", "10"], len(missing)))

        # 1b. After commits-years, resolve HEAD for repos with no usable per-year sha
        # (run unconditionally — it's cheap and skips already-resolved repos)
        plan.append(("resolve-head", ["uv", "run", "python", "-m", "src.sources.git.resolve_head", "--concurrency", "8"], 0))

        # 2. Resolve HEAD for dormant repos (no last_sha at any year)
        sha_data: dict[str, set[str]] = {}
        with (ROOT / "data/sources/git/commits-years.csv").open() as f:
            for r in csv.DictReader(f):
                if (r.get("last_sha") or "").strip():
                    sha_data.setdefault(r["repo"], set()).add(r.get("year", ""))
        dormant = eligible - set(sha_data.keys())
        if dormant:
            plan.append(("resolve-head", ["uv", "run", "python", "-m", "src.sources.git.resolve_head", "--concurrency", "8"], len(dormant)))

        # 3. Sha-pinned code metrics — ONE clone per repo yields scc.csv +
        # lizard.csv (scc loc/complexity + lizard cyclomatic + cognitive).
        scc_missing = eligible - _covered(ROOT / "data/sources/git/scc.csv")
        lizard_missing = eligible - _covered(ROOT / "data/sources/git/lizard.csv")
        if scc_missing or lizard_missing:
            plan.append(("sha-metrics", ["uv", "run", "python", "-m", "src.sources.git.fetch_sha_metrics", "--concurrency", "4"], len(scc_missing | lizard_missing)))

        openssf_missing = sorted(eligible - _covered(ROOT / "data/sources/git/openssf.csv"))
        if openssf_missing:
            tmp = ROOT / "/tmp/missing-openssf.txt"
            tmp.write_text("\n".join(openssf_missing) + "\n")
            plan.append(("openssf", ["uv", "run", "python", "-m", "src.sources.openssf.scorecard", "--file", str(tmp), "--concurrency", "5"], len(openssf_missing)))

        depsdev_missing = eligible - _covered(ROOT / "data/sources/git/depsdev.csv")
        if depsdev_missing:
            plan.append(("depsdev", ["uv", "run", "python", "-m", "src.sources.depsdev.fetch", "--concurrency", "20"], len(depsdev_missing)))

        issues_missing = eligible - _covered(ROOT / "data/sources/github/issues.csv")
        if issues_missing:
            plan.append(("issues", ["uv", "run", "python", "-m", "src.sources.github.fetch_issue_metrics"], len(issues_missing)))

        # 4. Regenerate openssf/checks.csv from data.json (free, fast)
        plan.append(("openssf-checks", ["uv", "run", "python", "-m", "src.sources.openssf.extract_checks"], 0))

    # 5. Build category CSVs + risk
    plan.append(("build_complexity",    ["uv", "run", "python", "-m", "src.risk.build_complexity"], 0))
    plan.append(("build_security",      ["uv", "run", "python", "-m", "src.risk.build_security"], 0))
    plan.append(("build_concentration", ["uv", "run", "python", "-m", "src.risk.build_concentration"], 0))
    plan.append(("build_funding",       ["uv", "run", "python", "-m", "src.eligibility.build_funding"], 0))
    plan.append(("build_workload",      ["uv", "run", "python", "-m", "src.risk.build_workload"], 0))
    plan.append(("risk",                ["uv", "run", "python", "-m", "src.risk.aggregate_risk"], 0))

    table = Table(title="Plan", show_header=True, header_style="bold dim")
    table.add_column("step")
    table.add_column("missing", justify="right")
    table.add_column("command", style="dim")
    for label, cmd, miss in plan:
        table.add_row(label, str(miss) if miss else "-", " ".join(cmd[3:]))
    console.print(table)

    if args.dry_run:
        return 0

    log_dir = Path("/tmp/fill-gaps")
    log_dir.mkdir(parents=True, exist_ok=True)

    failed: list[str] = []
    for label, cmd, _ in plan:
        console.rule(f"[bold]{label}[/bold]")
        rc = _run(cmd, log_dir / f"{label}.log")
        if rc == 0:
            console.print(f"[green]{label} OK[/green]")
        else:
            console.print(f"[red]{label} FAILED (rc={rc}). See {log_dir / (label + '.log')}[/red]")
            failed.append(label)

    if failed:
        console.print(f"\n[red]{len(failed)} step(s) failed: {failed}[/red]")
        return 1
    console.print("\n[green]All steps OK[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
