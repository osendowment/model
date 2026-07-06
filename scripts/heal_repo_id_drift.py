"""Heal non-canonical / drifted repo_ids in source CSVs. Idempotent.

Two repairs, matching the failure modes pipeline_health detects:

1. **Bare numerics → `gh/{id}`** — a fetcher wrote the raw GitHub API id
   without the `gh/` prefix (fixed at the source in
   `fetch_repo_owner_data._flat_repo`; this heals rows already on disk).
   Applies to every file listed in BARE_ID_FILES.

2. **Drifted ids → the value.csv id** — an id-joined source stamped a
   different id than value.csv assigns the slug (typically the GitHub-mirror
   id of a repo whose canonical home is GitLab), so the id-keyed builder
   join silently drops the rows. For each in-scope slug whose rows carry
   *no* row under the value.csv id, every row of that slug is restamped.
   Slugs that already have rows under the current id are left untouched —
   their stale-id rows are dead weight, not a join-drop, and restamping
   them could collide with the live rows.

Dry-run by default; `--write` applies. Run after a value-stage refresh
whenever pipeline_health's `id agreement` / `repo_id shape` checks fail.

Usage:
    uv run python scripts/heal_repo_id_drift.py           # report only
    uv run python scripts/heal_repo_id_drift.py --write   # apply
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Files known to accumulate bare numeric GitHub ids (raw API id, no prefix).
BARE_ID_FILES = [
    "sources/github/repos.csv",
    "sources/funding/host-by-repo.csv",
]

BARE_NUMERIC = re.compile(r"\d+")


def _value_id_by_slug() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(ROOT / "data" / "value" / "value.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("repo") or "").strip().lower()
            rid = (r.get("repo_id") or "").strip()
            if slug and rid:
                out[slug] = rid
    return out


def _rewrite(path: Path, fix_row, write: bool) -> int:
    """Stream `path`, apply fix_row(row) → bool(changed); rewrite if asked."""
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        rows = list(reader)
    changed = sum(1 for row in rows if fix_row(row))
    if changed and write:
        tmp = path.with_suffix(".healing")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(path)
    return changed


def heal_bare_ids(write: bool) -> list[tuple[str, int]]:
    """`{digits}` → `gh/{digits}` in BARE_ID_FILES."""
    out = []
    for rel in BARE_ID_FILES:
        path = ROOT / "data" / rel
        if not path.exists():
            continue

        def fix(row: dict) -> bool:
            rid = (row.get("repo_id") or "").strip()
            if rid and BARE_NUMERIC.fullmatch(rid):
                row["repo_id"] = f"gh/{rid}"
                return True
            return False

        out.append((rel, _rewrite(path, fix, write)))
    return out


def _id_joined_sources() -> list[str]:
    """ID_JOINED_SOURCES from pipeline_health.py (scripts/ is not a package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pipeline_health", Path(__file__).with_name("pipeline_health.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ID_JOINED_SOURCES


def heal_drift(write: bool) -> list[tuple[str, int, list[str]]]:
    """Restamp drifted-only in-scope slugs to the value.csv id."""
    from src.common.repos import load_top_repos

    scope = {e.repo for e in load_top_repos(skip_archived=False)}
    value_id = _value_id_by_slug()
    out = []
    for rel in _id_joined_sources():
        path = ROOT / "data" / rel
        if not path.exists():
            continue
        # First pass: which in-scope slugs have NO row under the value id?
        ids_by_slug: dict[str, set[str]] = {}
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = (row.get("repo") or "").strip().lower()
                rid = (row.get("repo_id") or "").strip()
                if rid and slug in scope and slug in value_id:
                    ids_by_slug.setdefault(slug, set()).add(rid)
        drifted = {s for s, ids in ids_by_slug.items() if value_id[s] not in ids}
        if not drifted:
            out.append((rel, 0, []))
            continue

        def fix(row: dict) -> bool:
            slug = (row.get("repo") or "").strip().lower()
            if slug in drifted and (row.get("repo_id") or "").strip() != value_id[slug]:
                row["repo_id"] = value_id[slug]
                return True
            return False

        n = _rewrite(path, fix, write)
        out.append((rel, n, sorted(drifted)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="Apply the repairs (default: dry-run report)")
    args = parser.parse_args()

    mode = "[bold]APPLYING[/bold]" if args.write else "[dim]dry-run[/dim]"
    console.print(f"\n[bold]repo_id heal[/bold] — {mode}\n")

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("File", style="bold")
    table.add_column("Repair")
    table.add_column("Rows", justify="right")
    table.add_column("Slugs", style="dim")
    total = 0
    for rel, n in heal_bare_ids(args.write):
        total += n
        table.add_row(rel, "gh/ prefix", str(n), "")
    for rel, n, slugs in heal_drift(args.write):
        total += n
        shown = ", ".join(slugs[:3]) + (" …" if len(slugs) > 3 else "")
        table.add_row(rel, "restamp to value id", str(n), shown)
    console.print(table)
    verb = "healed" if args.write else "would heal"
    console.print(f"\n{verb} [bold]{total}[/bold] row(s)."
                  + ("" if args.write else "  Re-run with --write to apply.") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
