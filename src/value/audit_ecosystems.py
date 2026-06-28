#!/usr/bin/env python3
"""Audit: compare ecosyste.ms's repo identity against our value.csv.

Read-only cross-check. For every class-A candidate package in
`data/sources/ecosystems/packages.csv` (produced by
`src.sources.ecosystems.candidates`), this compares the repository
ecosyste.ms resolved against the repository WE resolved — so we can measure how
often an independent source agrees with us before deciding whether to wire
ecosyste.ms into the git-URL grouping.

Sides being compared, both normalised to a canonical `(host, owner/repo)`:
    ours  — per-eco `results.csv` `github_repo` (bare owner/repo on GitHub),
            falling back to the `git` clone URL.
    eco   — `repo_host` + `repo_full_name` from packages.csv (ecosyste.ms's
            rename-resolved repo_metadata), falling back to `repository_url`.

Verdicts per package:
    match         host + owner/repo identical
    mismatch      both GitHub but different owner/repo (rename or wrong map)
    host_differs  eco points at a non-GitHub host (e.g. salsa.debian.org, gitlab)
    eco_only      we have no repo URL, ecosyste.ms found one
    ours_only     we have a repo URL, ecosyste.ms has none
    both_empty    neither side has anything

Writes `data/sources/ecosystems/audit.csv` (one row per package, with both
canonical slugs + verdict + whether our repo is present in value.csv) and prints
a `rich` summary. **value.csv is never modified.**

Usage:
    uv run python -m src.value.audit_ecosystems
    uv run python -m src.value.audit_ecosystems --show 40
"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.table import Table

log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
PACKAGES_CSV = DATA_DIR / "sources" / "ecosystems" / "packages.csv"
VALUE_CSV = DATA_DIR / "value" / "value.csv"
AUDIT_CSV = DATA_DIR / "sources" / "ecosystems" / "audit.csv"

ECOSYSTEMS = ["npm", "pypi", "crates", "cpp"]

# netloc → host kind, matching ecosyste.ms's repo_metadata.host.kind vocabulary.
HOST_KIND = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
    "codeberg.org": "codeberg",
    "git.sr.ht": "sourcehut",
    "sr.ht": "sourcehut",
}
AUDIT_FIELDS = ["ecosystem", "package", "verdict", "our_host", "our_repo",
                "eco_host", "eco_repo", "in_value_csv"]


# ── canonicalisation ──────────────────────────────────────────────────────────


def _from_slug(host: str, full_name: str) -> tuple[str, str]:
    """Canonicalise an already-structured (host_kind, owner/repo) pair."""
    return (host or "").strip().lower(), (full_name or "").strip().strip("/").lower()


def _from_url(url: str) -> tuple[str, str]:
    """Canonicalise a repo URL into (host_kind, owner/repo-or-path)."""
    url = (url or "").strip()
    if not url:
        return "", ""
    if "://" not in url:
        # bare "owner/repo" — our github_repo column form.
        return "github", url.strip("/").lower()
    p = urlparse(url)
    netloc = p.netloc.lower().removeprefix("www.")
    host = HOST_KIND.get(netloc, netloc)  # unknown host → keep the domain
    path = p.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segs = [s for s in path.split("/") if s]
    if host in {"github", "gitlab", "bitbucket", "codeberg", "sourcehut"} and len(segs) >= 2:
        path = f"{segs[0]}/{segs[1]}"
    return host, path.lower()


def _ours(row: dict) -> tuple[str, str]:
    """Our canonical repo for a results.csv row: github_repo, else git URL."""
    gh = (row.get("github_repo") or "").strip()
    if gh:
        return _from_slug("github", gh)
    return _from_url(row.get("git") or "")


def _eco(row: dict) -> tuple[str, str]:
    """ecosyste.ms's canonical repo: repo_metadata first, then repository_url."""
    full = (row.get("repo_full_name") or "").strip()
    if full:
        return _from_slug(row.get("repo_host") or "", full)
    return _from_url(row.get("repository_url") or "")


def _verdict(ours: tuple[str, str], eco: tuple[str, str]) -> str:
    o_host, o_repo = ours
    e_host, e_repo = eco
    if not o_repo and not e_repo:
        return "both_empty"
    if not o_repo:
        return "eco_only"
    if not e_repo:
        return "ours_only"
    if ours == eco:
        return "match"
    if e_host != "github":
        return "host_differs"
    return "mismatch"


# ── loaders ────────────────────────────────────────────────────────────────────


def _load_results() -> dict[tuple[str, str], dict]:
    """(ecosystem, package) -> results.csv row (our URLs)."""
    out: dict[tuple[str, str], dict] = {}
    for eco in ECOSYSTEMS:
        path = DATA_DIR / "sources" / eco / "results.csv"
        if not path.exists():
            continue
        for row in csv.DictReader(open(path, encoding="utf-8")):
            pkg = (row.get("package") or "").strip()
            if pkg:
                out[(eco, pkg)] = row
    return out


def _load_value_repos() -> set[str]:
    """Lowercased github_repo set from value.csv (membership check)."""
    if not VALUE_CSV.exists():
        return set()
    return {(r.get("github_repo") or "").strip().lower()
            for r in csv.DictReader(open(VALUE_CSV, encoding="utf-8"))
            if (r.get("github_repo") or "").strip()}


# ── audit ──────────────────────────────────────────────────────────────────────


def run_audit(show: int) -> dict[str, int]:
    if not PACKAGES_CSV.exists():
        console.print(f"[red]missing {PACKAGES_CSV} — run src.sources.ecosystems.candidates first[/red]")
        return {}

    results = _load_results()
    value_repos = _load_value_repos()
    pkgs = list(csv.DictReader(open(PACKAGES_CSV, encoding="utf-8")))

    rows: list[dict] = []
    counts: dict[str, int] = {}
    for pr in pkgs:
        eco, pkg = pr["ecosystem"], pr["package"]
        ours = _ours(results.get((eco, pkg), {}))
        eco_repo = _eco(pr)
        verdict = _verdict(ours, eco_repo)
        counts[verdict] = counts.get(verdict, 0) + 1
        rows.append({
            "ecosystem": eco, "package": pkg, "verdict": verdict,
            "our_host": ours[0], "our_repo": ours[1],
            "eco_host": eco_repo[0], "eco_repo": eco_repo[1],
            "in_value_csv": ours[1] in value_repos if ours[0] == "github" and ours[1] else False,
        })

    AUDIT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["verdict"], r["ecosystem"], r["package"])))

    _print_summary(rows, counts, show)
    return counts


def _print_summary(rows: list[dict], counts: dict[str, int], show: int) -> None:
    total = len(rows)
    order = ["match", "mismatch", "host_differs", "eco_only", "ours_only", "both_empty"]
    style = {"match": "green", "mismatch": "bold red", "host_differs": "yellow",
             "eco_only": "cyan", "ours_only": "dim", "both_empty": "dim"}

    tbl = Table(title="[bold]ecosyste.ms ↔ value.csv audit[/bold]  "
                      f"[dim]({total:,} candidate packages)[/dim]",
                show_header=True, header_style="bold dim", padding=(0, 1))
    tbl.add_column("Verdict", style="bold")
    tbl.add_column("Count", justify="right")
    tbl.add_column("%", justify="right")
    for v in order:
        n = counts.get(v, 0)
        if not n:
            continue
        tbl.add_row(f"[{style[v]}]{v}[/]", f"{n:,}", f"{100*n/total:.1f}%")
    tbl.add_section()
    tbl.add_row("[bold]Total[/bold]", f"[bold]{total:,}[/bold]", "100.0%")
    console.print()
    console.print(tbl)

    # The rows a human actually needs to look at, in priority order.
    interesting = [r for r in rows if r["verdict"] in ("mismatch", "host_differs", "eco_only")]
    interesting.sort(key=lambda r: (r["verdict"], r["ecosystem"], r["package"]))
    if interesting:
        det = Table(title=f"[bold]Disagreements & eco-only finds[/bold] "
                          f"[dim](showing {min(show, len(interesting))}/{len(interesting)})[/dim]",
                    show_header=True, header_style="bold dim", padding=(0, 1))
        for c in ("Verdict", "Eco", "Package", "Ours", "ecosyste.ms"):
            det.add_column(c)
        for r in interesting[:show]:
            det.add_row(f"[{style[r['verdict']]}]{r['verdict']}[/]", r["ecosystem"], r["package"][:28],
                        f"{r['our_host']}:{r['our_repo']}" if r["our_repo"] else "[dim]—[/]",
                        f"{r['eco_host']}:{r['eco_repo']}" if r["eco_repo"] else "[dim]—[/]")
        console.print()
        console.print(det)
        if len(interesting) > show:
            console.print(f"[dim]… +{len(interesting) - show} more in {AUDIT_CSV}[/dim]")
    console.print(f"[dim]wrote {AUDIT_CSV}[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--show", type=int, default=30,
                        help="Max disagreement rows to print (default: 30)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    run_audit(args.show)


if __name__ == "__main__":
    main()
