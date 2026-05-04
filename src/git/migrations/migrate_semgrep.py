"""Migrate `data/github/git/semgrep.csv` (sha-pinned, wide) into the
unified long format at `data/git/semgrep.csv`.

Schema in the destination (per `src.git.long_format`):

    repo, repo_id, commit_sha, metric, value, checked_at

Mapping
-------
- commit_sha  ← analyzed_sha
- checked_at  ← fetched_at
- repo_id     ← lookup in `data/eligibility-data.csv` (empty if missing)
- metric name is prefixed with the rulepack so multiple rulepacks for
  the same (repo, sha) coexist in one long file:
      p/default         → p_default
      p/security-audit  → p_security_audit
  e.g. rulepack=p/default + findings_total=5 →
       metric="p_default.findings_total", value=5
- numeric metrics migrated:
      findings_total, findings_error, findings_warning, findings_info,
      findings_security, findings_correctness, findings_performance,
      top_rule_count
- skipped columns: top_rule (string), elapsed_s, analyzed_year, rulepack
- rows where findings_total is empty are skipped (timeout/failure rows)

Run
---
    uv run python -m src.git.migrations.migrate_semgrep
"""
from __future__ import annotations

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.git.long_format import read as read_long
from src.git.long_format import upsert_snapshot

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE = REPO_ROOT / "data" / "github" / "git" / "semgrep.csv"
DEST = REPO_ROOT / "data" / "git" / "semgrep.csv"
ELIGIBILITY = REPO_ROOT / "data" / "eligibility-data.csv"

NUMERIC_METRICS: tuple[str, ...] = (
    "findings_total",
    "findings_error",
    "findings_warning",
    "findings_info",
    "findings_security",
    "findings_correctness",
    "findings_performance",
    "top_rule_count",
)


def rulepack_prefix(rulepack: str) -> str:
    """`p/security-audit` → `p_security_audit` (snake-case identifier)."""
    return rulepack.replace("/", "_").replace("-", "_").lower()


def load_repo_ids(path: Path) -> dict[str, str]:
    """Map repo → repo_id from eligibility-data.csv."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            repo = (row.get("repo") or "").strip()
            repo_id = (row.get("repo_id") or "").strip()
            if repo:
                out[repo] = repo_id
    return out


def parse_numeric(value: str) -> int | float | None:
    """Parse an int/float string; empty → None."""
    s = (value or "").strip()
    if s == "":
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def migrate() -> None:
    console = Console()
    console.rule("[bold]migrate_semgrep[/bold]")
    console.print(f"source: {SOURCE}")
    console.print(f"dest:   {DEST}")

    if not SOURCE.exists():
        console.print(f"[red]source missing: {SOURCE}[/red]")
        return

    repo_ids = load_repo_ids(ELIGIBILITY)
    console.print(f"loaded {len(repo_ids)} repo→repo_id mappings")

    # Wipe destination so we get a clean migrated file (callers can
    # re-run idempotently). `upsert_snapshot` will re-create it.
    if DEST.exists():
        DEST.unlink()

    table = Table(
        title="semgrep migration (per source row)",
        show_lines=False,
        header_style="dim",
    )
    table.add_column("repo")
    table.add_column("sha", width=9)
    table.add_column("rulepack")
    table.add_column("metrics", justify="right")

    src_rows = 0
    skipped_no_total = 0
    rulepacks_seen: set[str] = set()
    pairs_seen: set[tuple[str, str]] = set()
    # For round-trip validation: list of (repo, sha, metric, expected_str)
    expected: list[tuple[str, str, str, str]] = []

    with SOURCE.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            src_rows += 1
            repo = (row.get("repo") or "").strip()
            sha = (row.get("analyzed_sha") or "").strip()
            rulepack = (row.get("rulepack") or "").strip()
            checked_at = (row.get("fetched_at") or "").strip()

            if (row.get("findings_total") or "").strip() == "":
                skipped_no_total += 1
                continue
            if not repo or not sha or not rulepack:
                skipped_no_total += 1
                continue

            prefix = rulepack_prefix(rulepack)
            rulepacks_seen.add(rulepack)
            pairs_seen.add((repo, sha))

            metrics: dict[str, int | float] = {}
            for col in NUMERIC_METRICS:
                parsed = parse_numeric(row.get(col, ""))
                if parsed is None:
                    continue
                metrics[f"{prefix}.{col}"] = parsed

            repo_id = repo_ids.get(repo, "")
            upsert_snapshot(
                DEST,
                repo=repo,
                repo_id=repo_id,
                commit_sha=sha,
                metrics=metrics,
                checked_at=checked_at,
            )

            for m, v in metrics.items():
                # Reproduce long-format's storage rendering for ints/floats.
                if isinstance(v, float) and v.is_integer():
                    expected.append((repo, sha, m, str(int(v))))
                elif isinstance(v, int):
                    expected.append((repo, sha, m, str(v)))
                else:
                    expected.append((repo, sha, m, repr(v)))

            table.add_row(repo, sha[:7], rulepack, str(len(metrics)))

    console.print(table)

    # Round-trip validation: reload the destination and verify each
    # source row's metrics survive intact.
    reloaded = read_long(DEST)
    mismatches: list[str] = []
    for repo, sha, metric, want in expected:
        got = reloaded.get((repo, sha, metric), {}).get("value")
        if got != want:
            mismatches.append(
                f"  - ({repo}, {sha[:7]}, {metric}): want={want!r} got={got!r}"
            )

    summary = Table(title="summary", show_header=False, header_style="dim")
    summary.add_column("k")
    summary.add_column("v", justify="right")
    summary.add_row("source rows", str(src_rows))
    summary.add_row("skipped (empty findings_total)", str(skipped_no_total))
    summary.add_row("rows written to long file", str(len(reloaded)))
    summary.add_row("unique (repo, sha) pairs", str(len(pairs_seen)))
    summary.add_row("rulepacks observed", ", ".join(sorted(rulepacks_seen)))
    summary.add_row(
        "round-trip mismatches",
        f"[red]{len(mismatches)}[/red]" if mismatches else "0",
    )
    console.print(summary)

    if mismatches:
        console.print("[red]round-trip mismatches:[/red]")
        for m in mismatches[:20]:
            console.print(m)
        raise SystemExit(1)

    # Sample 5 output rows for the report.
    sample_keys = sorted(reloaded.keys())[:5]
    sample = Table(title="sample rows (data/git/semgrep.csv)", header_style="dim")
    for col in ("repo", "repo_id", "commit_sha", "metric", "value", "checked_at"):
        sample.add_column(col)
    for k in sample_keys:
        r = reloaded[k]
        sample.add_row(
            r["repo"],
            r["repo_id"],
            r["commit_sha"][:9],
            r["metric"],
            r["value"],
            r["checked_at"],
        )
    console.print(sample)


if __name__ == "__main__":
    migrate()
