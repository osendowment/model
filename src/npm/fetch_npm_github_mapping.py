"""Build data/npm/package-github-mapping.csv from nice-registry's
all-the-package-repos dataset (offline, no API calls).

The dataset is stored in node_modules/all-the-package-repos/data/packages.json
as a JSON object mapping package names to repo URLs.

Usage: uv run python src/npm/fetch_npm_github_mapping.py
"""

import csv
import json
import re

INPUT_CSV = "data/npm/top-package-downloads.csv"
OUTPUT_CSV = "data/npm/package-github-mapping.csv"
PACKAGES_JSON = "node_modules/all-the-package-repos/data/packages.json"

GITHUB_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?]|$)")


def github_repo(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL, or '' if not a GitHub URL."""
    m = GITHUB_RE.search(url) if url else None
    return m.group(1) if m else ""


def read_packages(filepath: str) -> list[str]:
    with open(filepath, encoding="utf-8") as f:
        return [row["package"] for row in csv.DictReader(f)]


print(f"Loading {PACKAGES_JSON} ...")
with open(PACKAGES_JSON, encoding="utf-8") as f:
    all_repos: dict[str, str | None] = json.load(f)

packages = read_packages(INPUT_CSV)

rows = [
    (pkg, github_repo(url := all_repos.get(pkg) or ""), url)
    for pkg in packages
]

with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["package", "github_repo", "repo_url"])
    writer.writerows(rows)

found = sum(1 for _, gh, _ in rows if gh)
print(f"Mapped {len(rows)} packages → {found} GitHub repos")
print(f"Output: {OUTPUT_CSV}")
