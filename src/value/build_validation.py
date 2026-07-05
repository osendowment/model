#!/usr/bin/env python3
"""Value pipeline step — aggregate git/GitHub validation into one audit table.

Refreshes the non-GitHub `git ls-remote` reachability cache, then rolls up
all validation signals:

  1. Refresh non-GitHub URL reachability via `_verify_non_github` (writes
     `data/sources/git/urls.csv`, TTL 365 days). Pass `--offline` to skip the
     refresh and use only the existing cache; `--refresh` to force re-check
     regardless of age.
  2. Collect every distinct validation *target* from `data/value/value.csv`:
     a github row's `repo` slug (type `github_repo`) or, for non-GitHub repos,
     its `git_url` (type `git_url`). Each target accumulates the `sources` —
     the ecosystems whose packages resolve to it.
  3. Load verdicts from the two caches:
     - `data/sources/github/repos.csv`  (`valid` + `fetched_at`), keyed by both
       the queried `repo` slug and the rename-resolved `full_name`.
     - `data/sources/git/urls.csv`      (`valid` + `checked_at`).
  4. Apply manual `valid` pins from `data/value/overrides.csv` (override wins).
  5. **Hard gate:** every target must have a verdict. A target with none is a
     pipeline error (run `verify_git_urls` first) — never silently invalid.
  6. Write `data/value/validation.csv` (target, type, sources, checked_at,
     valid) and join the per-repo `git_valid` verdict back into `value.csv`:
     `True` iff the repo has a validated github `repo` (mirror); `False`
     otherwise — no GitHub repo at all (orphan or non-GitHub-only upstream),
     or a github `repo` that 404s.

Usage:
    uv run python -m src.value.build_validation [--offline] [--refresh]
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.value.git_urls import _verify_non_github
from src.value.unify_value_data import (
    OUTPUT_FILE as VALUE_FILE,
    load_repo_overrides,
    write_value_data,
)

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
VALIDATION_FILE = DATA_DIR / "value" / "validation.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
GIT_URLS_FILE = DATA_DIR / "sources" / "git" / "urls.csv"

VALIDATION_FIELDS = ["target", "type", "sources", "checked_at", "valid"]


def _row_target(row: dict) -> tuple[str, str] | None:
    """Return a value row's single validation target `(target, type)`, or None.

    A row whose `platform` is `github` is validated via the GitHub API
    (type `github_repo`, keyed by its `repo` slug); a row with only a
    non-GitHub `git_url` is validated via `git ls-remote` (type `git_url`). A
    row with neither is an orphan. The github branch wins, so a github row's
    derived github `git_url` is never double-counted as a separate target.
    """
    repo = (row.get("repo") or "").strip().lower()
    gu = (row.get("git_url") or "").strip()
    if (row.get("platform") or "").strip().lower() == "github" and "/" in repo:
        return (repo, "github_repo")
    if gu:
        return (gu, "git_url")
    return None


def collect_targets(value_rows: list[dict]) -> dict[tuple[str, str], set[str]]:
    """Map each distinct target → the set of ecosystems referencing it.

    Sources come from each row's `ecosystems` column (comma-split). Multiple
    value rows resolving to the same target union their sources. Orphan rows
    (no target) contribute nothing.
    """
    targets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in value_rows:
        key = _row_target(row)
        if key is None:
            continue
        ecos = [e.strip() for e in (row.get("ecosystems") or "").split(",") if e.strip()]
        targets[key].update(ecos)
    return targets


def load_verdicts(
    gh_path: Path = GH_REPOS_FILE,
    git_path: Path = GIT_URLS_FILE,
) -> dict[tuple[str, str], dict]:
    """Read the two source caches → `{(target, type): {valid: bool, checked_at}}`.

    GitHub verdicts are keyed by BOTH the queried `repo` slug and the
    rename-resolved `full_name`, so a value row holding either form resolves.
    `valid` is parsed case-insensitively.
    """
    verdicts: dict[tuple[str, str], dict] = {}
    if os.path.exists(gh_path):
        with open(gh_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                verdict = {
                    "valid": (r.get("valid") or "").strip().lower() == "true",
                    "checked_at": (r.get("fetched_at") or "").strip(),
                }
                for slug in ((r.get("repo") or "").strip().lower(),
                             (r.get("full_name") or "").strip().lower()):
                    if slug:
                        verdicts[(slug, "github_repo")] = verdict
    if os.path.exists(git_path):
        with open(git_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                url = (r.get("url") or "").strip()
                if not url:
                    continue
                verdicts[(url, "git_url")] = {
                    "valid": (r.get("valid") or "").strip().lower() == "true",
                    "checked_at": (r.get("checked_at") or "").strip(),
                }
    return verdicts


def apply_overrides(
    verdicts: dict[tuple[str, str], dict],
    overrides: dict[tuple[str, str], dict],
) -> dict[tuple[str, str], dict]:
    """Force verdicts from `overrides.csv` `valid` pins (override wins).

    A pin resolves to the override's `repo` (github) target, else its `git_url`
    target. A pin with no resolvable target is skipped. Mutates and returns
    `verdicts`.
    """
    for (pkg, eco), ov in overrides.items():
        pin = (ov.get("valid") or "").strip()
        if not pin:
            continue
        if ov.get("repo"):
            key = (ov["repo"].strip().lower(), "github_repo")
        elif ov.get("git_url"):
            key = (ov["git_url"].strip().lower(), "git_url")
        else:
            console.print(
                f"[yellow]overrides.csv: {pkg}/{eco} pins valid={pin} but has "
                "no repo/git_url target — skipped[/yellow]"
            )
            continue
        verdicts[key] = {"valid": pin.lower() == "true", "checked_at": "override"}
    return verdicts


def build(
    targets: dict[tuple[str, str], set[str]],
    verdicts: dict[tuple[str, str], dict],
) -> tuple[list[dict], dict[tuple[str, str], bool]]:
    """Build validation rows + a `{target: valid}` map. Enforces the hard gate.

    Every target must have a verdict; a target with none raises SystemExit
    naming the offenders. Validation rows are sorted by (type, target).
    """
    missing = sorted(k for k in targets if k not in verdicts)
    if missing:
        listing = "\n".join(f"  {t}  [{ty}]" for t, ty in missing)
        raise SystemExit(
            f"build_validation: {len(missing)} target(s) have no validation "
            f"verdict:\n{listing}\n"
            "Run `uv run python -m src.value.verify_git_urls` first."
        )

    validation_rows: list[dict] = []
    target_valid: dict[tuple[str, str], bool] = {}
    for (target, vtype), sources in targets.items():
        v = verdicts[(target, vtype)]
        target_valid[(target, vtype)] = v["valid"]
        validation_rows.append({
            "target": target,
            "type": vtype,
            "sources": ",".join(sorted(sources)),
            "checked_at": v["checked_at"],
            "valid": str(v["valid"]),
        })
    validation_rows.sort(key=lambda r: (r["type"], r["target"]))
    return validation_rows, target_valid


def join_valid(
    value_rows: list[dict],
    target_valid: dict[tuple[str, str], bool],
) -> list[dict]:
    """Set each row's `git_valid`: True iff the repo has a validated GitHub repo.

    A repo is fundable only if it has a GitHub repo (or mirror) that resolves
    (HTTP 200). Everything else is `False`:
    - orphans (no upstream repo at all),
    - non-GitHub-only upstreams (sourceware / savannah / gitlab / …) — a
      non-GitHub `git_url` does not make a repo git_valid on its own; add a
      GitHub mirror to `overrides.csv` to bring one into scope,
    - a github `repo` that 404s.
    """
    for row in value_rows:
        repo = (row.get("repo") or "").strip().lower()
        is_github = (row.get("platform") or "").strip().lower() == "github"
        valid = is_github and bool(repo) and target_valid.get((repo, "github_repo")) is True
        row["git_valid"] = "True" if valid else "False"
    return value_rows


def _write_validation(rows: list[dict], path: Path = VALIDATION_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=VALIDATION_FIELDS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _print_summary(value_rows: list[dict], validation_rows: list[dict]) -> None:  # pragma: no cover
    from collections import Counter
    vc = Counter(r["git_valid"] for r in value_rows)
    table = Table(title="[bold]Value git_valid[/bold]",
                  header_style="bold dim", padding=(0, 1))
    table.add_column("git_valid"); table.add_column("rows", justify="right")
    table.add_row("[green]True[/green]", f"{vc.get('True', 0):,}")
    table.add_row("[red]False[/red]", f"{vc.get('False', 0):,}")
    table.add_row("[dim]empty (orphan)[/dim]", f"{vc.get('', 0):,}")
    table.add_section()
    table.add_row("[bold]Total[/bold]", f"[bold]{len(value_rows):,}[/bold]")
    console.print(table)
    tc = Counter(r["type"] for r in validation_rows)
    console.print(
        f"[dim]validation.csv: {len(validation_rows):,} targets "
        f"({tc.get('github_repo', 0):,} github, {tc.get('git_url', 0):,} git)"
        f" → {VALIDATION_FILE}[/dim]"
    )


def main() -> None:  # pragma: no cover
    import argparse
    parser = argparse.ArgumentParser(
        description="Build data/value/validation.csv and update value.csv git_valid column."
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Skip the non-GitHub ls-remote refresh; use only the existing cache.",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Force re-check all non-GitHub URLs regardless of cache age.",
    )
    args = parser.parse_args()

    console.print("[bold]Building validation.csv...[/bold]\n")
    with open(VALUE_FILE, encoding="utf-8") as f:
        value_rows = list(csv.DictReader(f))

    # Refresh the non-GitHub ls-remote reachability cache before rolling up.
    # Gate behind --offline (skip) and --refresh (force=True).
    nongithub_urls = sorted({
        (r.get("git_url") or "").strip()
        for r in value_rows
        if (r.get("platform") or "") != "github" and (r.get("git_url") or "").strip()
    })
    if not args.offline:
        _verify_non_github(nongithub_urls, force=args.refresh)

    targets = collect_targets(value_rows)
    verdicts = apply_overrides(load_verdicts(), load_repo_overrides())
    validation_rows, target_valid = build(targets, verdicts)

    _write_validation(validation_rows)
    join_valid(value_rows, target_valid)
    write_value_data(value_rows)

    _print_summary(value_rows, validation_rows)


if __name__ == "__main__":  # pragma: no cover
    main()
