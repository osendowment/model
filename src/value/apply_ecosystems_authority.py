#!/usr/bin/env python3
"""Apply ecosyste.ms as the authoritative repo-identity layer onto results.csv.

Roll-up step. For every package in each ecosystem's `results.csv`, re-resolve the
canonical `git` URL (and `github_repo` slug) using the source priority:

    override > eco_strong > prior > eco_weak

  • override   — curated `data/value/overrides.csv` (absolute; manual corrections
                 for the cases where ecosyste.ms contradicts ground truth)
  • eco_strong — ecosyste.ms `repo_full_name` (its rename-resolved canonical
                 GitHub repo); this is the authoritative signal — it follows
                 GitHub renames/moves, so it beats our prior registry slug
  • prior      — the `git` / `github_repo` already in results.csv (the native
                 registry resolution from `build_git_urls`, or a previous run)
  • eco_weak   — ecosyste.ms raw `repository_url` / `homepage` recorded WITHOUT a
                 successful repo crawl (dead / un-crawled / non-GitHub) — fallback

This reads `data/sources/ecosystems/packages.csv` (run
`src.sources.ecosystems.candidates --scope top` first) and the overrides file,
then rewrites each `results.csv`'s `git`, `github_repo` and `eco_guess` columns
in place. It needs NO native raw inputs (npm nice-registry, crates db-dump, …),
so unlike `build_git_urls` it runs inside `--rollup`. Idempotent: re-running
re-resolves from the same eco + override inputs.

Because the canonical `git` URL is the grouping key in `unify_value_data`, a
rename fix re-groups a repo correctly and a merge-override (several packages →
one repo) collapses them into a single value.csv row.

Usage:
    uv run python -m src.value.apply_ecosystems_authority
    uv run python -m src.value.apply_ecosystems_authority --eco npm
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.value.build_git_urls import (
    GIT_HOST_PRIORITY,
    PLATFORMS,
    _load_invalid_lookup,
    classify,
    merge_urls_with_source,
)
from src.value.unify_value_data import _github_repo_from_url, load_repo_overrides

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
PACKAGES_CSV = DATA_DIR / "sources" / "ecosystems" / "packages.csv"
ECOSYSTEMS = ["npm", "pypi", "crates", "cpp"]


# ── eco source ─────────────────────────────────────────────────────────────────


def load_eco(ecosystem: str) -> dict[str, dict[str, list[str]]]:
    """{package: {"strong": [...], "weak": [...]}} for one ecosystem from packages.csv.

    strong = rename-resolved canonical GitHub repo (`repo_full_name` @ github),
    weak   = raw repository_url / homepage (no successful crawl).
    """
    out: dict[str, dict[str, list[str]]] = {}
    if not PACKAGES_CSV.exists():
        return out
    with open(PACKAGES_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("ecosystem") != ecosystem:
                continue
            full = (r.get("repo_full_name") or "").strip()
            host = (r.get("repo_host") or "").strip().lower()
            strong = [f"https://github.com/{full}.git"] if full and host == "github" else []
            weak = [u for u in (r.get("repository_url"), r.get("homepage")) if (u or "").strip()]
            out[r["package"]] = {"strong": strong, "weak": weak}
    return out


# ── per-package resolution ──────────────────────────────────────────────────────


def resolve(prior_git: str, prior_github: str, strong: list[str], weak: list[str],
            ov: dict | None, is_invalid) -> tuple[str, str, str]:
    """Return (git_url, github_repo, eco_guess) under the authoritative priority."""
    prior: list[str] = []
    if prior_git:
        prior.append(prior_git)
    elif prior_github:
        prior.append(f"https://github.com/{prior_github}.git")

    merged, srcs = merge_urls_with_source(
        {"eco_strong": strong, "prior": prior, "eco_weak": weak}, is_invalid,
    )

    # Curated override is absolute.
    if ov:
        if ov.get("github_repo"):
            _, canon = classify(f"https://github.com/{ov['github_repo']}")
            if canon:
                merged["github"] = canon
                srcs["github"] = "override"
        elif ov.get("git_url"):
            merged["github"] = ""  # a git_url-only override drops any (dead) github slug
            plat, canon = classify(ov["git_url"])
            if plat:
                merged[plat] = canon
                srcs[plat] = "override"

    git_plat = next((p for p in GIT_HOST_PRIORITY if merged.get(p)), None)
    git = merged.get(git_plat, "") if git_plat else ""
    src = srcs.get(git_plat, "") if git_plat else ""

    # github_repo is authoritative from the winning git URL when it's GitHub;
    # a git_url-only override explicitly clears it; otherwise keep the prior slug
    # (non-GitHub canonical repos may still carry a github mirror downstream).
    if git_plat == "github":
        github = _github_repo_from_url(git)
    elif ov and ov.get("git_url"):
        github = ""
    else:
        github = prior_github

    guess = {"override": "override", "eco_strong": "eco",
             "eco_weak": "eco", "prior": "native"}.get(src, "")
    return git, github, guess


# ── per-ecosystem rewrite ───────────────────────────────────────────────────────


def apply_eco(ecosystem: str, overrides: dict, is_invalid) -> dict:
    results_path = DATA_DIR / "sources" / ecosystem / "results.csv"
    if not results_path.exists():
        return {"ecosystem": ecosystem, "total": 0}

    eco = load_eco(ecosystem)
    with open(results_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    for col in ("git", "github_repo", "eco_guess"):
        if col not in fields:
            fields.append(col)

    c = {"total": len(rows), "eco": 0, "override": 0, "git_changed": 0, "gh_changed": 0}
    for r in rows:
        pkg = r["package"]
        prior_git = (r.get("git") or "").strip().lower()
        prior_gh = (r.get("github_repo") or "").strip().lower()
        e = eco.get(pkg, {})
        git, gh, guess = resolve(
            prior_git, prior_gh, e.get("strong", []), e.get("weak", []),
            overrides.get((pkg, ecosystem)), is_invalid,
        )
        if guess == "eco":
            c["eco"] += 1
        elif guess == "override":
            c["override"] += 1
        if git != prior_git:
            c["git_changed"] += 1
        if gh != prior_gh:
            c["gh_changed"] += 1
        r["git"], r["github_repo"], r["eco_guess"] = git, gh, guess

    tmp = results_path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(results_path)
    return {"ecosystem": ecosystem, **c}


# ── CLI ──────────────────────────────────────────────────────────────────────


def _print(stats: list[dict]) -> None:
    t = Table(title="[bold]ecosyste.ms authoritative resolution[/bold]",
              show_header=True, header_style="bold dim", padding=(0, 1))
    t.add_column("Eco", style="bold")
    for col in ("Rows", "eco-won", "override", "git Δ", "github Δ"):
        t.add_column(col, justify="right")
    tot = {"total": 0, "eco": 0, "override": 0, "git_changed": 0, "gh_changed": 0}
    for s in stats:
        t.add_row(s["ecosystem"], f"{s.get('total',0):,}", f"{s.get('eco',0):,}",
                  f"{s.get('override',0):,}", f"[green]{s.get('git_changed',0):,}[/green]",
                  f"[green]{s.get('gh_changed',0):,}[/green]")
        for k in tot:
            tot[k] += s.get(k, 0)
    t.add_section()
    t.add_row("[bold]Total[/bold]", f"[bold]{tot['total']:,}[/bold]", f"[bold]{tot['eco']:,}[/bold]",
              f"[bold]{tot['override']:,}[/bold]", f"[bold green]{tot['git_changed']:,}[/bold green]",
              f"[bold green]{tot['gh_changed']:,}[/bold green]")
    console.print()
    console.print(t)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eco", choices=ECOSYSTEMS + ["all"], default="all")
    args = parser.parse_args()

    if not PACKAGES_CSV.exists():
        console.print(f"[red]missing {PACKAGES_CSV} — run "
                      "src.sources.ecosystems.candidates --scope top first[/red]")
        raise SystemExit(1)

    overrides = load_repo_overrides()
    is_invalid = _load_invalid_lookup()
    ecosystems = ECOSYSTEMS if args.eco == "all" else [args.eco]
    console.print(f"[bold]ecosyste.ms authoritative layer[/bold]  "
                  f"[dim]{len(overrides)} overrides | priority: override > eco_strong > prior > eco_weak[/dim]")
    stats = [apply_eco(e, overrides, is_invalid) for e in ecosystems]
    _print(stats)


if __name__ == "__main__":
    main()
