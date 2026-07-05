#!/usr/bin/env python3
"""Extract ecosyste.ms registry maintainers from already-cached package data.

ecosyste.ms's package response includes a `maintainers` array (registry-native
account owners with publish rights) that `src.sources.ecosystems.packages` /
`candidates` already fetch and cache to disk but never persist to a CSV. This
script re-reads that existing cache — no network calls — and writes one row
per (package, maintainer):

    data/sources/ecosystems/maintainers.csv:
        ecosystem, package, repo_id, login, name, email, role, uuid,
        html_url, fetched_at

`repo_id` is resolved from `data/sources/ecosystems/packages.csv`'s
`repo_full_name` via `src.common.repos.load_repo_ids()` (blank when the
package has no GitHub repo — non-GitHub host, or no match) — this file's
rows are keyed by a GitHub repo, so it carries `repo_id` per this repo's
repo-keyed source schema contract.

Only npm/pypi/crates.io carry a `maintainers` field on ecosyste.ms — cpp's
registries (Debian/vcpkg/spack/conan) do not, so cpp packages are skipped.

Usage:
    uv run python -m src.sources.ecosystems.fetch_maintainers
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import REPOS_FILE, load_repo_ids

log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
PACKAGES_FILE = DATA_DIR / "sources" / "ecosystems" / "packages.csv"
OUTPUT_FILE = DATA_DIR / "sources" / "ecosystems" / "maintainers.csv"

# Only these ecosystems' registries expose a `maintainers` array on
# ecosyste.ms; cpp's registries (debian/vcpkg/spack/conan) do not.
MAINTAINER_ECOSYSTEMS = ("npm", "pypi", "crates")

FIELDS = ["ecosystem", "package", "repo_id", "login", "name", "email", "role",
          "uuid", "html_url", "fetched_at"]


def _cache_path(data_dir: Path, eco: str, pkg: str) -> Path:
    """Where packages.py/candidates.py cached this package's raw ecosyste.ms JSON."""
    safe = pkg.replace("/", "__")
    return data_dir / "sources" / eco / "raw" / "ecosystems" / f"{safe}.json"


def _read_cached(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _candidate_packages(packages_file: Path) -> list[tuple[str, str, str]]:
    """(ecosystem, package, repo_full_name) for every maintainer-capable
    ecosystem row already indexed in packages.csv."""
    out: list[tuple[str, str, str]] = []
    if not packages_file.exists():
        return out
    with open(packages_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            eco = (row.get("ecosystem") or "").strip()
            if eco not in MAINTAINER_ECOSYSTEMS:
                continue
            pkg = (row.get("package") or "").strip()
            if pkg:
                out.append((eco, pkg, (row.get("repo_full_name") or "").strip()))
    return out


def _maintainer_rows_from_cache(
    eco: str, pkg: str, repo_id: str, wrapper: dict,
) -> list[dict[str, str]]:
    """Flatten one cached ecosyste.ms wrapper's `maintainers` array into rows.

    `wrapper` is the `{"ecosystem", "registry_hit", "fetched_at", "data": {...}}`
    shape written by `packages._fetch_one` / `candidates._row_from_cache`.
    Maintainer entries with no `login` are skipped (nothing to key them by).
    """
    data = (wrapper or {}).get("data") or {}
    fetched_at = (wrapper or {}).get("fetched_at", "") or ""
    rows = []
    for m in data.get("maintainers") or []:
        login = (m.get("login") or "").strip()
        if not login:
            continue
        rows.append({
            "ecosystem": eco,
            "package": pkg,
            "repo_id": repo_id,
            "login": login,
            "name": (m.get("name") or "").strip(),
            "email": (m.get("email") or "").strip(),
            "role": (m.get("role") or "").strip(),
            "uuid": str(m.get("uuid") or "").strip(),
            "html_url": (m.get("html_url") or "").strip(),
            "fetched_at": fetched_at,
        })
    return rows


def build(
    data_dir: Path = DATA_DIR,
    packages_file: Path = PACKAGES_FILE,
    repos_file: str = REPOS_FILE,
) -> list[dict[str, str]]:
    repo_ids = load_repo_ids(repos_file)
    rows: list[dict[str, str]] = []
    n_no_cache = 0
    for eco, pkg, full_name in _candidate_packages(packages_file):
        wrapper = _read_cached(_cache_path(data_dir, eco, pkg))
        if wrapper is None:
            n_no_cache += 1
            continue
        repo_id = repo_ids.get(full_name.lower(), "") if full_name else ""
        rows.extend(_maintainer_rows_from_cache(eco, pkg, repo_id, wrapper))
    if n_no_cache:
        log.debug("skipped %d packages with no cached ecosyste.ms data", n_no_cache)
    return rows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    console.print("[bold]Extracting ecosyste.ms maintainers from cache...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    with_repo = sum(1 for r in rows if r["repo_id"])
    table = Table(title="[bold]Maintainer extraction[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Maintainer rows", f"{len(rows):,}")
    table.add_row("With resolved repo_id", f"{with_repo:,}")
    console.print(table)
    console.print(f"\n[dim]Wrote {len(rows):,} rows -> {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
