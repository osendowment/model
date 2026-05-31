#!/usr/bin/env python3
"""Build data/risk/funding.csv — funding signals + risk score per risk-scope repo.

Reads (all under data/sources/):
    github/sponsors.csv        — github_sponsors (inbound)  (src.github.fetch_sponsors)
    github/sponsorships.csv    — sponsoring_count (outbound) (src.github.fetch_sponsorships)
    github/funding-yml.csv     — has_funding_yml / platforms  (src.github.fetch_funding_yml)
    github/repos.csv           — stars, forks (info)          (src.github.fetch_repo_owner_data)
    floss-fund/funding-json.csv — FLOSS Fund directory export  (src.floss_fund.funding_json)
    opencollective/budgets.csv — annual gross raised per OC slug (src.opencollective.fetch_budgets)
    foundations/host-by-repo.csv — FOSS-foundation host per repo

Writes data/risk/funding.csv. The funding risk **score** (0-100, higher =
less funded = riskier) is the geometric mean of two direction-aware risk
percentiles:
    gh_sponsorships_p  ← gh_sponsorships (in + out), lower → riskier
    oc_avg_funding_p   ← oc_avg_funding ($0 when none),  lower → riskier
`gh_stars` / `gh_forks` are informational popularity columns (not scored);
their fetch timestamp lives in data/sources/github/repos.csv. No per-signal
`fetched_at` is rolled up here.

Usage:
    uv run python -m src.pipeline.risk.build_funding
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.floss_fund.directory import normalize_github_repo
from src.pipeline.common.funding_platforms import normalize_oc_slug
from src.pipeline.common.percentiles import add_percentiles
from src.pipeline.common.repos import load_risk_repos
from src.pipeline.common.tables import load_column_by_repo, load_rows_by_repo

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
SPONSORS_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
SPONSORSHIPS_FILE = DATA_DIR / "sources" / "github" / "sponsorships.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
FLOSS_FUND_FILE = DATA_DIR / "sources" / "floss-fund" / "funding-json.csv"
OC_BUDGETS_FILE = DATA_DIR / "sources" / "opencollective" / "budgets.csv"
FOUNDATIONS_FILE = DATA_DIR / "sources" / "foundations" / "host-by-repo.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "funding.csv"

FIELDS = ["repo", "repo_id",
          "gh_sponsors_in", "gh_sponsors_out", "gh_sponsorships", "gh_sponsorships_p",
          "gh_stars", "gh_forks",
          "has_funding_yml", "funding_yml_platforms", "has_funding_json", "channels_count",
          "oc_avg_funding", "oc_avg_funding_p",
          "foundation_host", "score"]


def _platform_set(csv_value: str) -> set[str]:
    return {p.strip() for p in (csv_value or "").split(",") if p.strip()}


def _to_int(s: str) -> int:
    try:
        return int((s or "").strip())
    except ValueError:
        return 0


def oc_avg_funding(slug: str, oc_budgets: dict[str, dict]) -> str:
    """Mean of an Open Collective slug's gross annual budgets ($0 when none).

    Absence of OC funding is treated as $0, so the value is numeric for every
    repo and participates in `oc_avg_funding_p`.
    """
    row = oc_budgets.get(slug) if slug else None
    if not row:
        return "0"
    vals = [float(v) for k, v in row.items()
            if k.startswith("raised_") and (v or "").strip()]
    if not vals:
        return "0"
    avg = sum(vals) / len(vals)
    return str(int(avg)) if avg == int(avg) else f"{avg:.2f}"


def assemble_row(repo: str, repo_id: str, sponsors: dict, yml: dict, export: dict,
                 foundation_host: str, oc_budgets: dict, repo_meta: dict,
                 sponsoring_count: str = "") -> dict:
    """Join one repo's raw funding signals (percentiles filled later in build())."""
    channels = _platform_set(yml.get("funding_yml_platforms")) | _platform_set(
        export.get("channel_platforms"))
    oc_slug = (normalize_oc_slug(yml.get("open_collective"))
               or normalize_oc_slug(export.get("open_collective")))
    gh_in = (sponsors.get("github_sponsors") or "").strip()
    gh_out = (sponsoring_count or "").strip()
    return {
        "repo": repo,
        "repo_id": repo_id,
        "gh_sponsors_in": gh_in,
        "gh_sponsors_out": gh_out,
        "gh_sponsorships": str(_to_int(gh_in) + _to_int(gh_out)),
        "gh_stars": (repo_meta.get("stars") or "").strip(),
        "gh_forks": (repo_meta.get("forks") or "").strip(),
        "has_funding_yml": (yml.get("has_funding_yml") or "").strip(),
        "funding_yml_platforms": (yml.get("funding_yml_platforms") or "").strip(),
        "has_funding_json": "True" if export else "False",
        "channels_count": str(len(channels)),
        "oc_avg_funding": oc_avg_funding(oc_slug, oc_budgets),
        "foundation_host": foundation_host,
    }


def _export_by_repo(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = normalize_github_repo(row.get("project_repository"))
            if repo:
                out[repo] = row
    return out


def _load_oc(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = (row.get("slug") or "").strip().lower()
                if slug:
                    out[slug] = row
    return out


def _load_sponsoring(path: Path) -> dict[str, str]:
    """{owner_login: sponsoring_count} from sponsorships.csv."""
    out: dict[str, str] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                login = (row.get("login") or "").strip().lower()
                if login:
                    out[login] = (row.get("sponsoring_count") or "").strip()
    return out


def build() -> list[dict]:
    eligible = load_risk_repos()
    sponsors = load_rows_by_repo(SPONSORS_FILE)
    yml = load_rows_by_repo(FUNDING_YML_FILE)
    repos_meta = load_rows_by_repo(REPOS_FILE)
    foundations = load_column_by_repo(FOUNDATIONS_FILE, "host")
    export = _export_by_repo(FLOSS_FUND_FILE)
    oc_budgets = _load_oc(OC_BUDGETS_FILE)
    sponsoring = _load_sponsoring(SPONSORSHIPS_FILE)

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        owner = repo.split("/", 1)[0].lower()
        rows.append(assemble_row(
            repo=repo, repo_id=entry.repo_id,
            sponsors=sponsors.get(repo, {}), yml=yml.get(repo, {}),
            export=export.get(repo.lower(), {}),
            foundation_host=foundations.get(repo, ""),
            oc_budgets=oc_budgets, repo_meta=repos_meta.get(repo, {}),
            sponsoring_count=sponsoring.get(owner, "")))

    # Funding risk score: both axes are `lower_is_worse` (less funding → riskier);
    # score = geometric mean of the two risk percentiles (0-100, integer).
    add_percentiles(
        rows,
        pctl_specs=[("gh_sponsorships", False), ("oc_avg_funding", False)],
        composite_cols=["gh_sponsorships_p", "oc_avg_funding_p"],
        dim_col="score",
    )
    return rows


def main() -> None:
    console.print("[bold]Building funding.csv...[/bold]\n")
    rows = build()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Funding coverage[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    for col in ("gh_sponsorships", "gh_stars", "has_funding_yml", "has_funding_json",
                "channels_count", "oc_avg_funding", "foundation_host", "score"):
        n = sum(1 for r in rows if str(r.get(col, "")) not in ("", "0", "False"))
        table.add_row(col, f"{n:,}")
    console.print(table)

    fh = Counter(r["foundation_host"] for r in rows if r["foundation_host"])
    if fh:
        ftable = Table(title="\n[bold]Foundation hosts[/bold]", show_header=True,
                       header_style="bold dim", padding=(0, 1))
        ftable.add_column("Host", style="bold")
        ftable.add_column("Repos", justify="right")
        for host, n in sorted(fh.items(), key=lambda x: -x[1]):
            ftable.add_row(host, f"{n:,}")
        console.print(ftable)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
