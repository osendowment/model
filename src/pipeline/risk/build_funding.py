#!/usr/bin/env python3
"""Build data/risk/funding.csv — funding signals per risk-scope repo.

Reads (all under data/sources/):
    github/sponsors.csv        — github_sponsors  (src.github.fetch_sponsors)
    github/funding-yml.csv     — has_funding_yml / funding_yml_platforms /
                                 per-platform handles  (src.github.fetch_funding_yml)
    floss-fund/funding-json.csv — FLOSS Fund directory export; has_funding_json
                                 + channel_platforms  (src.floss_fund.funding_json)
    opencollective/budgets.csv — annual gross raised per OC slug
                                 (src.opencollective.fetch_budgets)
    foundations/host-by-repo.csv — FOSS-foundation host per repo

Writes data/risk/funding.csv:
    repo, repo_id, gh_sponsors_in, gh_sponsors_out, gh_sponsorships,
    gh_sponsorships_p, has_funding_yml, funding_yml_platforms, has_funding_json,
    channels_count, oc_avg_funding, oc_avg_funding_p, funding_p, foundation_host,
    fetched_at

`has_funding_json` is True iff the repo is registered in the FLOSS Fund
directory. `channels_count` is the number of distinct funding platforms across
FUNDING.yml and the funding.json channels (deduped union). `oc_avg_funding` is
the mean of the repo's Open Collective gross annual budgets ($0 when none).
`gh_sponsorships_p` / `oc_avg_funding_p` are inverted Hazen funding-risk
percentiles (less funding → higher percentile); `funding_p` is their geometric
mean (the dimension's overall funding-risk percentile). Only `oc_avg_funding`,
`gh_sponsorships` and `funding_p` are carried into risk.csv. `fetched_at` is the
most recent contributing timestamp.

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
from src.pipeline.common.repos import load_risk_repos
from src.pipeline.common.stats import geometric_mean, hazen_percentiles
from src.pipeline.common.tables import load_column_by_repo, load_rows_by_repo

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
SPONSORS_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
SPONSORSHIPS_FILE = DATA_DIR / "sources" / "github" / "sponsorships.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
FLOSS_FUND_FILE = DATA_DIR / "sources" / "floss-fund" / "funding-json.csv"
OC_BUDGETS_FILE = DATA_DIR / "sources" / "opencollective" / "budgets.csv"
FOUNDATIONS_FILE = DATA_DIR / "sources" / "foundations" / "host-by-repo.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "funding.csv"

FIELDS = ["repo", "repo_id", "gh_sponsors_in", "gh_sponsors_out",
          "gh_sponsorships", "gh_sponsorships_p", "has_funding_yml",
          "funding_yml_platforms", "has_funding_json", "channels_count",
          "oc_avg_funding", "oc_avg_funding_p", "funding_p",
          "foundation_host", "fetched_at"]


def _latest(*timestamps: str) -> str:
    """Most recent ISO timestamp among the args (lexical order works for ISO)."""
    return max((t for t in timestamps if t), default="")


def _platform_set(csv_value: str) -> set[str]:
    return {p.strip() for p in (csv_value or "").split(",") if p.strip()}


def oc_avg_funding(slug: str, oc_budgets: dict[str, dict]) -> str:
    """Mean of an Open Collective slug's gross annual budgets (years with data).

    Returns "0" when the repo has no Open Collective presence or no recorded
    budget — absence of OC funding is treated as $0, so the value is numeric
    for every repo and participates in `oc_avg_funding_p`.
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


def _to_int(s: str) -> int:
    s = (s or "").strip()
    try:
        return int(s)
    except ValueError:
        return 0


def _to_float(s: str) -> float:
    try:
        return float((s or "").strip())
    except ValueError:
        return 0.0


def _assign_risk_pctl(rows: list[dict], value_key: str, pctl_key: str) -> None:
    """Fill `pctl_key` with the inverted Hazen percentile of `value_key`.

    Inverted: a LOWER value (less funding) → HIGHER risk percentile (mirrors
    the negated openssf_score in build_security). Every repo is ranked — both
    metrics are numeric for all repos (sponsorships default to 0, OC funding
    defaults to $0).
    """
    pctls = hazen_percentiles([-_to_float(r[value_key]) for r in rows])
    for r, p in zip(rows, pctls):
        r[pctl_key] = round(p, 2)


def assemble_row(repo: str, repo_id: str, sponsors: dict, yml: dict, export: dict,
                 foundation_host: str, oc_budgets: dict, sponsoring_count: str = "") -> dict:
    """Join one repo's funding signals into a funding.csv row.

    `gh_sponsors_in`  = inbound GitHub sponsors received (from sponsors.csv).
    `gh_sponsors_out` = the owner's outbound sponsoring (looked up by login in
                        `build()` — an account-level signal, not per-repo).
    `gh_sponsorships` = in + out: total GitHub-sponsorship engagement (either
                        direction signals a resourced project). `_p` percentiles
                        are filled by `build()` once every repo's value is known.
    """
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
        "gh_sponsorships_p": "",  # filled in build() once all totals are known
        "has_funding_yml": (yml.get("has_funding_yml") or "").strip(),
        "funding_yml_platforms": (yml.get("funding_yml_platforms") or "").strip(),
        "has_funding_json": "True" if export else "False",
        "channels_count": str(len(channels)),
        "oc_avg_funding": oc_avg_funding(oc_slug, oc_budgets),
        "oc_avg_funding_p": "",  # filled in build()
        "funding_p": "",         # geom-mean of the two _p columns, filled in build()
        "foundation_host": foundation_host,
        "fetched_at": _latest((sponsors.get("fetched_at") or "").strip(),
                              (yml.get("fetched_at") or "").strip()),
    }


def _export_by_repo(path: Path) -> dict[str, dict]:
    """FLOSS Fund export rows keyed by normalized `owner/repo`."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = normalize_github_repo(row.get("project_repository"))
            if repo:
                out[repo] = row
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
            oc_budgets=oc_budgets,
            sponsoring_count=sponsoring.get(owner, "")))

    # Funding risk percentiles (inverted: less funding → higher risk), one per
    # channel. `funding_p` is their geometric mean — the funding dimension's
    # overall risk percentile (low if the repo is funded on EITHER channel).
    _assign_risk_pctl(rows, "gh_sponsorships", "gh_sponsorships_p")
    _assign_risk_pctl(rows, "oc_avg_funding", "oc_avg_funding_p")
    for r in rows:
        r["funding_p"] = round(geometric_mean(
            [float(r["gh_sponsorships_p"]), float(r["oc_avg_funding_p"])]), 2)
    return rows


def _load_oc(path: Path) -> dict[str, dict]:
    """budgets.csv keyed by OC slug."""
    out: dict[str, dict] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = (row.get("slug") or "").strip().lower()
                if slug:
                    out[slug] = row
    return out


def main() -> None:
    console.print("[bold]Building funding.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Funding coverage[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in ("gh_sponsors_in", "gh_sponsors_out", "has_funding_yml",
                "funding_yml_platforms", "has_funding_json", "channels_count",
                "oc_avg_funding", "foundation_host"):
        n = sum(1 for r in rows if r[col] and r[col] not in ("False", "0"))
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
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
