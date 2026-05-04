"""One-shot migration: OpenSSF Scorecard data.json → data/git/openssf.csv.

Reads the canonical raw cache `data/openssf/data.json` (a JSON object
keyed by `owner/repo`, each value a Scorecard scan result) and writes
the long-format file:

    repo, repo_id, commit_sha, metric, value, checked_at

Metrics emitted per entry:
  - `score`             ← top-level Scorecard score (0..10, may be float)
  - one row per check   ← metric name = snake_case(check.name), value = score
                          (e.g. `Binary-Artifacts` → `binary_artifacts`,
                          `CII-Best-Practices` → `cii_best_practices`)

Check scores of -1 are kept (they're a real signal: "check not applicable").
Only None/null/missing scores are skipped.

Source files (read-only):
  - data/openssf/data.json       (canonical raw scorecard cache)
  - data/eligibility-data.csv    (repo → repo_id lookup)

Destination:
  - data/git/openssf.csv (created fresh; aborts if it already exists)

Run:
  uv run python -m src.git.migrations.migrate_openssf
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.git.long_format import _format_value
from src.git.long_format import read as read_long
from src.git.long_format import upsert_rows

ROOT = Path(__file__).resolve().parents[3]
DATA_JSON = ROOT / "data" / "openssf" / "data.json"
ELIGIBILITY_CSV = ROOT / "data" / "eligibility-data.csv"
DEST_CSV = ROOT / "data" / "git" / "openssf.csv"


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


def to_snake_case(name: str) -> str:
    """`Binary-Artifacts` → `binary_artifacts`, `CII-Best-Practices` → `cii_best_practices`.

    Hyphens become underscores; everything is lowercased. We don't try to
    insert separators inside CamelCase tokens because Scorecard check names
    are already hyphen-separated (e.g. `SAST`, `CII-Best-Practices`).
    """
    s = name.strip().replace("-", "_")
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s.lower()


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
    console.rule("[bold]migrate_openssf[/]")
    console.print(f"src data.json       : {DATA_JSON}")
    console.print(f"eligibility (ids)   : {ELIGIBILITY_CSV}")
    console.print(f"destination         : {DEST_CSV}\n")

    if DEST_CSV.exists():
        console.print(f"[red]ABORT[/] destination already exists: {DEST_CSV}")
        console.print("Refusing to overwrite. Move/delete it manually if you really want to redo.")
        return 1

    repo_ids = load_repo_ids(ELIGIBILITY_CSV)
    console.print(f"loaded {len(repo_ids)} repo→repo_id mappings from eligibility")

    with DATA_JSON.open(encoding="utf-8") as f:
        data = json.load(f)
    console.print(f"loaded {len(data)} entries from data.json\n")

    total = len(data)
    errored = 0
    missing_commit = 0
    migrated = 0
    pairs: set[tuple[str, str]] = set()
    distinct_metrics: set[str] = set()
    sample_repos: list[str] = []
    new_rows: list[dict[str, str]] = []

    # Accumulate all rows in memory, then do ONE upsert_rows() call to write
    # the file once. Per-entry upsert would be O(n^2) over 2.5k entries.
    for repo_key, entry in data.items():
        if not isinstance(entry, dict):
            errored += 1
            continue
        if entry.get("error"):
            errored += 1
            continue

        repo_obj = entry.get("repo") or {}
        commit_sha = (repo_obj.get("commit") or "").strip()
        if not commit_sha:
            missing_commit += 1
            continue

        checked_at = (entry.get("date") or "").strip()
        repo_id = repo_ids.get(repo_key, "")

        # Build metric dict: top-level `score` + one entry per check.
        metrics: dict[str, object] = {}
        top_score = entry.get("score")
        if top_score is not None:
            metrics["score"] = top_score
            distinct_metrics.add("score")

        for check in entry.get("checks") or []:
            name = (check or {}).get("name")
            score = (check or {}).get("score")
            if not name or score is None:
                continue
            metric_name = to_snake_case(name)
            metrics[metric_name] = score
            distinct_metrics.add(metric_name)

        if not metrics:
            # No usable scores at all — skip this entry.
            continue

        for metric, value in metrics.items():
            formatted = _format_value(value)
            if formatted == "":
                continue
            new_rows.append({
                "repo": repo_key,
                "repo_id": str(repo_id) if repo_id else "",
                "commit_sha": commit_sha,
                "metric": metric,
                "value": formatted,
                "checked_at": checked_at,
            })

        migrated += 1
        pairs.add((repo_key, commit_sha))
        if len(sample_repos) < 3:
            sample_repos.append(repo_key)

    # One-shot batched write.
    upsert_rows(DEST_CSV, new_rows)

    # Validate — read back the destination.
    rows = read_long(DEST_CSV)
    written = len(rows)

    # Stats table.
    console.print()
    table = Table(title="migration stats", show_lines=False)
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    table.add_row("total entries in JSON", str(total))
    table.add_row("errored entries", str(errored))
    table.add_row("entries missing commit", str(missing_commit))
    table.add_row("[bold]entries migrated[/]", f"[bold]{migrated}[/]")
    table.add_row("unique (repo, sha) pairs", str(len(pairs)))
    table.add_row("distinct check metrics observed", str(len(distinct_metrics)))
    table.add_row("[bold]rows written[/]", f"[bold]{written}[/]")
    console.print(table)

    # Distinct metrics list (alphabetised, comma-separated).
    console.print()
    console.print("[bold]distinct metrics:[/]")
    console.print(", ".join(sorted(distinct_metrics)))

    # Spot-check 3 sample repos: reconstruct {metric: score} and compare
    # visually against the source JSON.
    console.print()
    console.rule("[bold]spot-checks[/]")
    for repo_key in sample_repos:
        recon = reconstruct(rows, repo_key)
        entry = data[repo_key]
        src_metrics: dict[str, object] = {}
        if entry.get("score") is not None:
            src_metrics["score"] = entry["score"]
        for check in entry.get("checks") or []:
            name = (check or {}).get("name")
            score = (check or {}).get("score")
            if not name or score is None:
                continue
            src_metrics[to_snake_case(name)] = score

        console.print(f"\n[cyan]{repo_key}[/]")
        for sha, m in recon.items():
            console.print(f"  csv  sha={sha[:10]}…")
            console.print(f"    {dict(sorted(m.items()))}")
        console.print(f"  json source:")
        console.print(f"    {dict(sorted(src_metrics.items()))}")

    # First 5 rows of the new CSV.
    console.print()
    console.rule("[bold]sample (first 5 rows of data/git/openssf.csv)[/]")
    with DEST_CSV.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            console.print(line.rstrip())
            if i >= 5:
                break

    return 0


if __name__ == "__main__":
    sys.exit(main())
