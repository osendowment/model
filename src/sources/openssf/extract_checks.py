#!/usr/bin/env python3
"""Extract per-check sub-scores from data/sources/openssf/data.json into a wide CSV.

OpenSSF Scorecard returns a top-level aggregate `score` (0-10) plus per-check
scores for ~18 individual checks (Maintained, Code-Review, CI-Tests,
Branch-Protection, Vulnerabilities, etc.). The aggregate is in the
sha-pinned long file `data/sources/git/openssf.csv`; the per-check breakdown also
lives in `data.json`.

This script flattens the JSON into a wide CSV so downstream pipeline stages
can join per-check scores by repo without parsing JSON.

Pure transform — no network I/O, so it takes no --offline/--refresh and needs
no TTL. It runs as a plain pipeline step AFTER the `scorecard` fetcher (which
writes data.json) and is safe to re-run: the output is deterministic (rows
sorted by repo, one row per repo) and written atomically.

Reads:
    data/sources/openssf/data.json   {repo: {checks: [{name, score}, ...], ...}}

Writes:
    data/sources/openssf/checks.csv  with columns:
        repo, repo_id, score, <Check-Name-1>, <Check-Name-2>, ..., date

Where each check column is the integer score 0..10, or `-1` (the upstream
sentinel for "not applicable / unable to evaluate"), or empty if the check
wasn't run for that repo.

Missing / unusable input (see main): an absent data.json is a no-op warning
(the scorecard step has not run yet), while a corrupt one is a hard error. In
neither case is checks.csv overwritten — `build_workload` treats a missing or
empty checks.csv as "no Maintained signal for any repo", so a truncated write
here would silently zero the openssf_maintained dimension.

Usage:
    uv run python -m src.sources.openssf.extract_checks
"""

import csv
import json
import os
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import load_repo_ids, load_value_repo_ids

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
INPUT_FILE = DATA_DIR / "sources" / "openssf" / "data.json"
OUTPUT_FILE = DATA_DIR / "sources" / "openssf" / "checks.csv"

# Standard scorecard checks (stable ordering for the wide CSV).
CHECK_NAMES = [
    "Binary-Artifacts",
    "Branch-Protection",
    "CI-Tests",
    "CII-Best-Practices",
    "Code-Review",
    "Contributors",
    "Dangerous-Workflow",
    "Dependency-Update-Tool",
    "Fuzzing",
    "License",
    "Maintained",
    "Packaging",
    "Pinned-Dependencies",
    "SAST",
    "Security-Policy",
    "Signed-Releases",
    "Token-Permissions",
    "Vulnerabilities",
    "Webhooks",
]


def build(data: dict) -> tuple[list[dict], int]:
    """Flatten the scorecard payload into wide rows. Returns (rows, skipped).

    Deterministic and duplicate-free: repo keys are lowercased to a slug, and if
    two keys collapse to the same slug (`Owner/Repo` vs `owner/repo`) the newest
    `date` wins, so one repo yields exactly one row no matter the key casing.
    `skipped` counts malformed payloads — a partial data.json still contributes
    every well-formed repo it does have.
    """
    # repos.csv (keyed on both `repo` and rename-resolved `full_name`) overlaid on
    # value.csv ids so a repo renamed since either file was written still resolves;
    # repos.csv wins on conflict.
    repo_ids = {**load_value_repo_ids(), **load_repo_ids()}

    by_slug: dict[str, dict] = {}
    skipped = 0
    for repo_key, payload in sorted(data.items()):
        if not isinstance(payload, dict):
            skipped += 1
            continue
        slug = repo_key.strip().lower()
        # The repo key is already in `owner/name` form.
        checks = payload.get("checks")
        per_check = {c.get("name"): c.get("score")
                     for c in checks if isinstance(c, dict)} if isinstance(checks, list) else {}
        row = {
            "repo": slug,
            "repo_id": repo_ids.get(slug, ""),
            "score": payload.get("score", ""),
            "date": payload.get("date", ""),
        }
        for name in CHECK_NAMES:
            v = per_check.get(name)
            row[name] = "" if v is None else v
        prior = by_slug.get(slug)
        if prior is None or str(row["date"]) >= str(prior["date"]):
            by_slug[slug] = row

    rows = sorted(by_slug.values(), key=lambda r: r["repo"])
    return rows, skipped


def main() -> None:
    console.print("[bold]Extracting OpenSSF per-check scores...[/bold]\n")

    # ── input gates ───────────────────────────────────────────────────────────
    # checks.csv is never overwritten on a bad input: build_workload reads a
    # missing/empty checks.csv as "no Maintained signal at all", so a truncated
    # write would silently zero that dimension for every repo.
    if not INPUT_FILE.exists():
        console.print(f"[yellow]WARNING:[/yellow] {INPUT_FILE} not found — the scorecard "
                      f"fetcher has not run yet. Nothing to extract; leaving "
                      f"{OUTPUT_FILE.name} untouched.")
        return  # exit 0 — a not-yet-fetched input is not a pipeline failure

    try:
        with open(INPUT_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        # A corrupt data.json is a genuine bug (the fetcher writes it atomically),
        # not a "not yet run" state. Fail loudly rather than let a stale
        # checks.csv masquerade as freshly rebuilt.
        raise SystemExit(f"ERROR: {INPUT_FILE} is not valid JSON ({e}); "
                         f"refusing to rewrite {OUTPUT_FILE.name}. Re-run the scorecard fetcher.")
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {INPUT_FILE} is not a JSON object (got {type(data).__name__}); "
                         f"refusing to rewrite {OUTPUT_FILE.name}.")

    rows, skipped = build(data)

    if skipped:
        console.print(f"[yellow]WARNING:[/yellow] skipped {skipped:,} malformed "
                      f"repo payload(s) in {INPUT_FILE.name}\n")

    # Zero usable rows would blank the dimension — never overwrite real data with it.
    if not rows and OUTPUT_FILE.exists() and os.path.getsize(OUTPUT_FILE) > 0:
        raise SystemExit(f"ERROR: {INPUT_FILE.name} yielded 0 usable repos but "
                         f"{OUTPUT_FILE.name} holds data; refusing to truncate it.")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fields = ["repo", "repo_id", "score", *CHECK_NAMES, "date"]
    tmp = OUTPUT_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, OUTPUT_FILE)  # atomic — a crash mid-write can't truncate checks.csv

    total = len(rows)
    table = Table(title="[bold]Per-check coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Check", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for name in CHECK_NAMES:
        n = sum(1 for r in rows if r[name] not in ("", None))
        pct = 100 * n / total if total else 0
        table.add_row(name, f"{n:,}", f"{pct:.1f}%")
    console.print(table)
    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
