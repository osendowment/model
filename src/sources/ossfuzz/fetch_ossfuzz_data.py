"""
Fetch the list of projects fuzzed by Google's OSS-Fuzz.

OSS-Fuzz is Google's continuous fuzzing service for critical open-source
software. Its project list is a high-quality, hand-curated whitelist of
projects considered important enough to fuzz — strong signal for
"actually-used C/C++ infrastructure worth funding".

Source: github.com/google/oss-fuzz/projects/<name>/project.yaml

Output:
    data/sources/ossfuzz/projects.csv    project, language, github_repo, repo_id,
                                         main_repo, homepage, fetched_at

Run:
    uv run python -m src.sources.ossfuzz.fetch_ossfuzz_data
"""

import argparse
import csv
import io
import os
import re
import tarfile
import time
from datetime import datetime, timezone

import requests
import yaml
from rich.console import Console
from rich.table import Table

from src.common.repos import load_repo_ids, load_value_repo_ids

# ── config ────────────────────────────────────────────────────────────────────

OUT_PATH = "data/sources/ossfuzz/projects.csv"
TARBALL = "https://codeload.github.com/google/oss-fuzz/tar.gz/refs/heads/master"
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"
TIMEOUT = 300

GITHUB_RE = re.compile(r"github\.com[:/]([^/\s#?]+)/([^/\s#?.]+)", re.IGNORECASE)

console = Console()


def atomic_write(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _repo_id_map() -> dict[str, str]:
    """Slug -> stable GitHub numeric id: github/repos.csv first, value.csv fallback.

    Carrying the stable `repo_id` alongside the `github_repo` slug makes the
    downstream enrollment join rename-proof. Many OSS-Fuzz projects are out of
    model scope and resolve to nothing — those keep a blank repo_id (an id is
    never invented).
    """
    ids = load_repo_ids()
    for slug, rid in load_value_repo_ids().items():
        ids.setdefault(slug, rid)
    return ids


def extract_github_slug(*urls: str) -> str:
    for url in urls:
        if not url:
            continue
        m = GITHUB_RE.search(url)
        if m:
            owner, repo = m.group(1), m.group(2)
            if repo.endswith(".git"):
                repo = repo[:-4]
            return f"{owner}/{repo}"
    return ""


def fetch_ossfuzz_projects() -> list[dict]:
    console.rule("[bold cyan]OSS-Fuzz — fetching project list")
    t0 = time.perf_counter()
    console.print(f"  Downloading {TARBALL} …")
    r = requests.get(TARBALL, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, stream=True)
    r.raise_for_status()
    data = r.content
    console.print(f"  {len(data) / 1e6:.1f} MB in {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    projects: list[dict] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf:
            # Path looks like: oss-fuzz-master/projects/<name>/project.yaml
            name = member.name
            if not (name.endswith("/project.yaml") and "/projects/" in name):
                continue
            parts = name.split("/")
            # Need exactly one path segment between "projects" and "project.yaml"
            try:
                i = parts.index("projects")
            except ValueError:
                continue
            if len(parts) != i + 3:
                continue
            project = parts[i + 1]

            fobj = tf.extractfile(member)
            if not fobj:
                continue
            try:
                meta = yaml.safe_load(fobj.read()) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(meta, dict):
                continue

            homepage  = (meta.get("homepage")  or "").strip()
            main_repo = (meta.get("main_repo") or "").strip()
            language  = (meta.get("language")  or "").strip().lower()
            slug = extract_github_slug(main_repo, homepage)
            projects.append({
                "project":     project,
                "language":    language,
                "github_repo": slug,
                "main_repo":   main_repo,
                "homepage":    homepage,
            })
    console.print(f"  Parsed {len(projects):,} project.yaml files in {time.perf_counter() - t0:.1f}s")
    projects.sort(key=lambda p: p["project"])
    return projects


def main() -> None:
    argparse.ArgumentParser().parse_args()

    console.rule("[bold]ossfuzz — fetch_ossfuzz_data")
    console.print(f"  Started : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]\n")

    projects = fetch_ossfuzz_projects()

    # Enrich: stable repo_id (blank when the slug is out of model scope) and
    # one UTC fetch timestamp for the whole run.
    repo_ids = _repo_id_map()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for p in projects:
        p["repo_id"] = repo_ids.get(p["github_repo"].lower(), "")
        p["fetched_at"] = fetched_at

    atomic_write(OUT_PATH, projects,
                 ["project", "language", "github_repo", "repo_id",
                  "main_repo", "homepage", "fetched_at"])

    lang_counts: dict[str, int] = {}
    with_gh = with_id = 0
    for p in projects:
        lang_counts[p["language"] or "unknown"] = lang_counts.get(p["language"] or "unknown", 0) + 1
        if p["github_repo"]:
            with_gh += 1
        if p["repo_id"]:
            with_id += 1

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Total projects", f"{len(projects):,}")
    tbl.add_row("With github slug", f"{with_gh:,} ({100 * with_gh / max(len(projects),1):.1f}%)")
    tbl.add_row("With repo_id", f"{with_id:,} ({100 * with_id / max(len(projects),1):.1f}%)")
    for lang, count in sorted(lang_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]:
        tbl.add_row(f"  language={lang}", f"{count:,}")
    console.print(tbl)
    console.print(f"  → wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
