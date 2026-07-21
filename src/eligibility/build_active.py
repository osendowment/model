#!/usr/bin/env python3
"""Build data/eligibility/active.csv — is each top repo still alive?

active = NOT eol AND NOT archived. There is no mirror exemption: an archived
GitHub repo is inactive, full stop. A project whose canonical upstream lives
off GitHub (e.g. glibc on sourceware, pixman on gitlab.freedesktop.org) is
pointed at that upstream in data/value/overrides.csv rather than exempted here.

- **`eol`** — the manual end-of-life call from data/eligibility/overrides.csv
  (`eol` column, True = project is end-of-life; empty/absent = alive). The
  per-ecosystem registry EOL signals (data/sources/<eco>/eol.csv, produced by
  src.sources.<eco>.check_eol) are advisory inputs to that manual call — they
  flag *packages* (deprecations, yanks), while `eol` is a per-repo verdict.
- **`archived`** — the host's `archived` flag, read off the RepoEntry that
  load_top_repos hands over. GitHub rows come from data/sources/github/repos.csv
  (src.sources.github.fetch_repo_owner_data); GitLab rows enrich from
  data/sources/gitlab/repos.csv (src.sources.gitlab.fetch_project_data), so
  `gl/` repos carry a real verdict rather than a default False.

Usage:
    uv run python -m src.eligibility.build_active
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import load_top_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OVERRIDES_FILE = DATA_DIR / "eligibility" / "overrides.csv"
OUTPUT_FILE = DATA_DIR / "eligibility" / "active.csv"

FIELDS = ["repo", "repo_id", "eol", "archived", "active"]


def load_eol_overrides(
    path: Path = OVERRIDES_FILE,
) -> tuple[dict[str, bool], dict[str, bool]]:
    """(by_repo_id, by_slug) eol verdicts from the stage overrides.

    Only rows with a non-empty `eol` cell are returned — True marks a
    project declared end-of-life, an explicit False pins a repo alive.
    `repo_id` is the join key (rename-proof: the override's `repo` slug
    drifts after a GitHub rename, exactly like the funding columns that
    `_load_funding_overrides` already joins by id); the slug map is the
    fallback for hand-added rows without an id.
    """
    by_id: dict[str, bool] = {}
    by_slug: dict[str, bool] = {}
    if not path.exists():
        return by_id, by_slug
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            eol = (row.get("eol") or "").strip()
            if not eol:
                continue
            rid = (row.get("repo_id") or "").strip()
            slug = (row.get("repo") or "").strip().lower()
            if rid:
                by_id[rid] = eol == "True"
            elif slug:
                by_slug[slug] = eol == "True"
    return by_id, by_slug


def build() -> list[dict]:
    # Eligibility scope includes archived repos (load_top_repos includes
    # them by default) — this builder is exactly where they surface as
    # active=False instead of being silently dropped from the stage output.
    eligible = load_top_repos()
    eol_by_id, eol_by_slug = load_eol_overrides()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        eol = eol_by_id.get(str(entry.repo_id),
                            eol_by_slug.get(repo.lower(), False))
        active = (not eol) and (not entry.archived)
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "eol": eol,
            "archived": entry.archived,
            "active": active,
        })
    return rows


def main() -> None:
    console.print("[bold]Building active.csv...[/bold]\n")
    rows = build()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Activity[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("", style="bold")
    table.add_column("Repos", justify="right")
    table.add_column("%", justify="right")
    for label, pred, style in (
            ("active", lambda r: r["active"], "green"),
            ("eol (override)", lambda r: r["eol"], "red"),
            ("archived", lambda r: r["archived"], "red")):
        n = sum(1 for r in rows if pred(r))
        table.add_row(f"[{style}]{label}[/{style}]", f"{n:,}", f"{100 * n / total:.1f}%")
    table.add_section()
    table.add_row("[bold]Total[/bold]", f"[bold]{total:,}[/bold]", "")
    console.print(table)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
