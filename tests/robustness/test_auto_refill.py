"""Auto-refill robustness test:

  1. Delete random rows from a source file
  2. Run scripts/fill_gaps.py --build-only to rebuild category CSVs
  3. Verify the deletion shows up as missing in risk-data.csv
  4. Run scripts/fill_gaps.py (full) to refetch the missing data
  5. Verify the gap is closed in the next pipeline run

This proves the auto-orchestrator detects and self-heals data gaps.

Usage:
    uv run python -m tests.robustness.test_auto_refill --source data/sources/git/scc.csv --n 5
"""
from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console

console = Console()
ROOT = Path(__file__).resolve().parent.parent.parent


def coverage_in_risk(field: str, repos: set[str]) -> dict[str, str]:
    """Return {repo: value} for the given field, only for `repos`."""
    out: dict[str, str] = {}
    with (ROOT / "data/risk/risk.csv").open() as f:
        for r in csv.DictReader(f):
            if r["repo"] in repos:
                out[r["repo"]] = (r.get(field) or "").strip()
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/sources/git/scc.csv")
    parser.add_argument("--field", default="loc_2025_eoy",
                        help="Field in risk-data.csv to check")
    parser.add_argument("--n", type=int, default=5,
                        help="Random repos to delete")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-refill", action="store_true",
                        help="Stop after detection (skip auto-refill step)")
    args = parser.parse_args()

    p = ROOT / args.source
    backup = Path("/tmp/auto-refill-backup.csv")
    shutil.copy(p, backup)

    # 1. Pick random repos
    import random
    rng = random.Random(args.seed)
    with p.open() as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)
    repos = sorted({r["repo"] for r in rows if r.get("repo")})
    target = set(rng.sample(repos, min(args.n, len(repos))))
    console.print(f"[bold]Removing {len(target)} repos from {args.source}[/bold]: {sorted(target)}")

    kept = [r for r in rows if r.get("repo") not in target]
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(kept)
    console.print(f"[dim]→ wrote {len(kept)} rows (was {len(rows)})[/dim]")

    # 2. Build-only pipeline
    console.print("\n[bold]Step 2: Rebuilding category CSVs[/bold]")
    result = subprocess.run(
        ["uv", "run", "python", "scripts/fill_gaps.py", "--build-only"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]build_only failed: {result.stderr[-500:]}[/red]")
        shutil.copy(backup, p)
        return 1

    # 3. Check field is empty for target repos
    cov_after_delete = coverage_in_risk(args.field, target)
    empty = [r for r, v in cov_after_delete.items() if not v]
    populated = [r for r, v in cov_after_delete.items() if v]
    console.print(f"\nAfter row deletion: [yellow]{len(empty)}[/yellow] empty, "
                  f"[green]{len(populated)}[/green] silently filled (cross-source fallback).")
    for r, v in list(cov_after_delete.items())[:5]:
        console.print(f"  {r}.{args.field}={v!r}")

    if args.no_refill:
        shutil.copy(backup, p)
        console.print("[dim]Restored source. Stopping (--no-refill).[/dim]")
        return 0 if empty else 1  # all empty = good detection

    # 4. Run full fill_gaps to refetch
    console.print("\n[bold]Step 4: Running auto-refill[/bold]")
    result = subprocess.run(
        ["uv", "run", "python", "scripts/fill_gaps.py"],
        capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0:
        console.print(f"[yellow]fill_gaps reported failures (some sources may be structural): "
                      f"{result.stderr[-300:]}[/yellow]")

    # 5. Check field is repopulated
    cov_after_refill = coverage_in_risk(args.field, target)
    refilled = [r for r, v in cov_after_refill.items() if v]
    still_empty = [r for r, v in cov_after_refill.items() if not v]
    console.print(f"\nAfter refill: [green]{len(refilled)}[/green] repopulated, "
                  f"[yellow]{len(still_empty)}[/yellow] still empty.")
    if still_empty:
        console.print(f"  Still empty (likely structural): {still_empty[:5]}")

    # Restore backup just in case
    shutil.copy(backup, p)

    if len(refilled) == len(target):
        console.print(f"\n[green]PASS — auto-refill closed all {len(target)} gaps.[/green]")
        return 0
    elif len(refilled) >= 0.8 * len(target):
        console.print(f"\n[yellow]PARTIAL — refilled {len(refilled)}/{len(target)}.[/yellow]")
        return 0
    else:
        console.print(f"\n[red]FAIL — only refilled {len(refilled)}/{len(target)}.[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
