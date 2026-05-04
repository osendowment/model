"""Auto-fill missing data per source for all eligible repos, then rebuild
category CSVs and risk-data.

Detects gaps by comparing eligible repos against each source file's
unique repo set, then invokes the appropriate fetcher. Each fetcher has
its own smart-upsert / TTL semantics, so re-running is cheap if there
are no gaps.

Usage:
    uv run python scripts/fill_gaps.py
    uv run python scripts/fill_gaps.py --skip-semgrep   # skip the slow one
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


def _run(cmd: list[str], log_path: Path) -> int:
    """Run a fetcher, tee its output to a log file, return exit code."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    return proc.returncode


def _eligible() -> set[str]:
    out = set()
    with (ROOT / "data/eligibility-data.csv").open() as f:
        for r in csv.DictReader(f):
            if (r.get("eligibility") or "").strip() == "True":
                out.add(r["repo"])
    return out


def _covered(path: Path, repo_col: str = "repo") -> set[str]:
    if not path.exists():
        return set()
    with path.open() as f:
        return {r[repo_col] for r in csv.DictReader(f) if r.get(repo_col)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-semgrep", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    eligible = _eligible()
    console.print(f"[bold]Eligible:[/bold] {len(eligible)} repos\n")

    plan: list[tuple[str, list[str], int]] = []  # (label, cmd, missing_count)

    if not args.build_only:
        # 1. Foundation: commits-years for any repos missing
        missing = eligible - _covered(ROOT / "data/github/git/commits-years.csv")
        if missing:
            plan.append(("commits-years", ["uv", "run", "python", "-m", "src.git.commits_years", "--concurrency", "10"], len(missing)))

        # 1b. After commits-years, resolve HEAD for repos with no usable per-year sha
        # (run unconditionally — it's cheap and skips already-resolved repos)
        plan.append(("resolve-head", ["uv", "run", "python", "-m", "src.git.resolve_head", "--concurrency", "8"], 0))

        # 2. Resolve HEAD for dormant repos (no last_sha at any year)
        sha_data: dict[str, set[str]] = {}
        with (ROOT / "data/github/git/commits-years.csv").open() as f:
            for r in csv.DictReader(f):
                if (r.get("last_sha") or "").strip():
                    sha_data.setdefault(r["repo"], set()).add(r.get("year", ""))
        dormant = eligible - set(sha_data.keys())
        if dormant:
            plan.append(("resolve-head", ["uv", "run", "python", "-m", "src.git.resolve_head", "--concurrency", "8"], len(dormant)))

        # 3. Sha-pinned fetchers
        scc_missing = eligible - _covered(ROOT / "data/git/scc.csv")
        if scc_missing:
            plan.append(("scc", ["uv", "run", "python", "-m", "src.git.fetch_scc", "--concurrency", "4"], len(scc_missing)))

        lizard_missing = eligible - _covered(ROOT / "data/git/lizard.csv")
        if lizard_missing:
            plan.append(("cognitive", ["uv", "run", "python", "-m", "src.github.fetch_cognitive", "--concurrency", "3"], len(lizard_missing)))
            plan.append(("cyclo-halstead", ["uv", "run", "python", "-m", "src.github.fetch_advanced_complexity", "--limit", "0", "--concurrency", "3"], len(lizard_missing)))

        openssf_missing = sorted(eligible - _covered(ROOT / "data/git/openssf.csv"))
        if openssf_missing:
            tmp = ROOT / "/tmp/missing-openssf.txt"
            tmp.write_text("\n".join(openssf_missing) + "\n")
            plan.append(("openssf", ["uv", "run", "python", "-m", "src.openssf.scorecard", "--file", str(tmp), "--concurrency", "5"], len(openssf_missing)))

        depsdev_missing = eligible - _covered(ROOT / "data/git/depsdev.csv")
        if depsdev_missing:
            plan.append(("depsdev", ["uv", "run", "python", "-m", "src.depsdev.fetch", "--concurrency", "20"], len(depsdev_missing)))

        churn_missing = eligible - _covered(ROOT / "data/github/git/churn.csv")
        if churn_missing:
            plan.append(("churn", ["uv", "run", "python", "-m", "src.github.fetch_churn", "--concurrency", "4"], len(churn_missing)))

        issues_missing = eligible - _covered(ROOT / "data/github/issues.csv")
        if issues_missing:
            plan.append(("issues", ["uv", "run", "python", "-m", "src.github.fetch_issue_metrics"], len(issues_missing)))

        if not args.skip_semgrep:
            semgrep_missing = eligible - _covered(ROOT / "data/git/semgrep.csv")
            if semgrep_missing:
                plan.append(("semgrep", ["uv", "run", "python", "-m", "src.github.fetch_semgrep", "--limit", "0", "--rulepack", "p/default", "--concurrency", "4"], len(semgrep_missing)))

        # 4. Regenerate openssf/checks.csv from data.json (free, fast)
        plan.append(("openssf-checks", ["uv", "run", "python", "-m", "src.openssf.extract_checks"], 0))

    # 5. Build category CSVs + risk
    plan.append(("build_complexity",    ["uv", "run", "python", "-m", "src.pipeline.build_complexity"], 0))
    plan.append(("build_security",      ["uv", "run", "python", "-m", "src.pipeline.build_security"], 0))
    plan.append(("build_concentration", ["uv", "run", "python", "-m", "src.pipeline.build_concentration"], 0))
    plan.append(("build_funding",       ["uv", "run", "python", "-m", "src.pipeline.build_funding"], 0))
    plan.append(("build_visibility",    ["uv", "run", "python", "-m", "src.pipeline.build_visibility"], 0))
    plan.append(("build_workload",      ["uv", "run", "python", "-m", "src.pipeline.build_workload"], 0))
    plan.append(("risk",                ["uv", "run", "python", "-m", "src.pipeline.risk"], 0))

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
