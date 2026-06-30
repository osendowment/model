#!/usr/bin/env python3
"""Build data/risk/funding.csv — funding signals + risk score per risk-scope repo.

Reads (all under data/sources/):
    github/sponsors.csv        — github_sponsors (inbound)  (src.github.fetch_sponsors)
    github/sponsorships.csv    — sponsoring_count (outbound) (src.github.fetch_sponsorships)
    github/funding-yml.csv     — has_funding_link / platforms (src.github.fetch_funding_yml)
    github/repos.csv           — stars, forks (info)          (src.github.fetch_repo_owner_data)
    floss-fund/funding-json.csv — FLOSS Fund directory export  (src.sources.floss_fund.funding_json)
    opencollective/budgets.csv — annual gross raised per OC slug (src.sources.opencollective.fetch_budgets)
    funding/host-by-repo.csv   — scraped FOSS-foundation host per repo
    funding/overrides.csv      — curated host/owner institutional backing per repo
    npm/funding.csv            — npm package.json `funding` field (npm repos only,
                                 src.sources.npm.fetch_funding) — a declared channel
    pypi/funding.csv           — PyPI `project_urls` funding link (pypi repos only,
                                 src.sources.pypi.fetch_funding) — a declared channel

Writes data/risk/funding.csv. The funding risk **score** (0-100, higher =
less funded = riskier) is the geometric mean of THREE direction-aware risk axes:
    gh_sponsorships_p  ← gh_sponsorships (in + out), lower → riskier
    oc_avg_funding_p   ← oc_avg_funding ($0 when none),  lower → riskier
    host_score×100     ← combined host/owner backing (company 0, nonprofit 50, none 100)
A repo that DECLARES a funding channel — a registry channel (`has_npm_funding` /
`has_pypi_funding`) or a fundable owner/org (`org_fundable`) — has its score
capped at DECLARED_FUNDING_CAP (79): it is not maximally unfunded even when no $
is measured. npm/pypi are repo/package-level (catching channels the owner-level
checks miss); `org_fundable` is owner-level (a FLOSS Fund manifest registered for
the whole GitHub org).
`gh_stars` / `gh_forks` are informational popularity columns (not scored);
their fetch timestamp lives in data/sources/github/repos.csv. No per-signal
`fetched_at` is rolled up here. The script also writes two boolean flag columns:
`intent` (True when ≥1 funding signal is present) and `nonprofit` (False only
when host_type or owner_type == "company"). These flags are joined into
data/risk/risk.csv by aggregate_risk.py.

Usage:
    uv run python -m src.risk.build_funding
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.funding_platforms import normalize_oc_slug
from src.common.percentiles import add_percentiles
from src.common.repos import load_top_repos
from src.common.stats import geometric_mean
from src.common.tables import load_column_by_repo, load_rows_by_repo
from src.sources.floss_fund.directory import export_repo_slug, github_org_page
from src.sources.opencollective.fetch_collectives import load_index as _load_oc_index

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SPONSORS_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
SPONSORSHIPS_FILE = DATA_DIR / "sources" / "github" / "sponsorships.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
FLOSS_FUND_FILE = DATA_DIR / "sources" / "floss-fund" / "funding-json.csv"
OC_BUDGETS_FILE = DATA_DIR / "sources" / "opencollective" / "budgets.csv"
FOUNDATIONS_FILE = DATA_DIR / "sources" / "funding" / "host-by-repo.csv"
OVERRIDES_FILE = DATA_DIR / "sources" / "funding" / "overrides.csv"
NPM_FUNDING_FILE = DATA_DIR / "sources" / "npm" / "funding.csv"
PYPI_FUNDING_FILE = DATA_DIR / "sources" / "pypi" / "funding.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "funding.csv"

# A repo that DECLARES a funding channel whose $ we cannot measure is not
# maximally unfunded even if no $ is observed — cap its funding risk at the "has
# some backing" level (the nonprofit-host score: geom(100,100,50) ≈ 79).
DECLARED_FUNDING_CAP = 79

# Platforms whose dollars we DO measure: GitHub Sponsors (sponsors.csv counts)
# and Open Collective (budgets.csv). For these the score already reflects the
# real signal, so a link to them is NOT a cap trigger. Every other funding-link
# platform (liberapay, ko_fi, patreon, tidelift, custom, …) is unmeasured — a
# link is the only signal we have, so it caps (same logic as npm/pypi/org).
MEASURED_PLATFORMS = {"github", "open_collective"}

FIELDS = ["repo", "repo_id",
          "gh_sponsors_in", "gh_sponsors_out", "owner_has_sponsors_listing",
          "gh_sponsorships", "gh_sponsorships_p",
          "gh_stars", "gh_forks",
          "has_funding_link", "funding_link_platforms", "has_funding_json", "org_fundable",
          "has_npm_funding", "npm_funding_url",
          "has_pypi_funding", "pypi_funding_platforms", "channels_count",
          "oc_slug", "oc_avg_funding", "oc_avg_funding_p",
          "host", "host_type", "owner", "owner_type", "host_score",
          "score", "intent", "nonprofit"]

# A repo's institutional backing discounts its funding risk. `host` = the
# foundation/company *legally* stewarding the project; `owner` = the entity that
# owns the GitHub org. Each is typed company / nonprofit / (none). The single
# `host_score` combines both — the most-funded backer wins (min) — and is the
# multiplier carried into the funding score: a company backer fully resources the
# project (0 → score floors at 1), a nonprofit/foundation halves the risk (0.5),
# and no known backer leaves it unchanged (1).
TYPE_SCORE = {"company": 0.0, "nonprofit": 0.5, "": 1.0}


def _type_score(t: str) -> float:
    return TYPE_SCORE.get((t or "").strip().lower(), 1.0)


def _backing_score(host_type: str, owner_type: str) -> float:
    """Combined backing multiplier ∈ {0, 0.5, 1} — the most-funded of host/owner."""
    return min(_type_score(host_type), _type_score(owner_type))


def _nonprofit_flag(host_type: str, owner_type: str) -> bool:
    """Default True; False only when a corporate host or owner backs the repo."""
    return "company" not in (
        (host_type or "").strip().lower(),
        (owner_type or "").strip().lower(),
    )


def _intent_flag(row: dict) -> bool:
    """Sustainability intent: True if the repo shows any funding signal — a live
    sponsorship, the owner's GitHub Sponsors listing, a declared channel
    (FUNDING.yml / funding.json / npm / PyPI / OC), or an institutional host/owner."""
    return (
        _to_int(row.get("gh_sponsors_in")) + _to_int(row.get("gh_sponsors_out")) > 0
        or (row.get("owner_has_sponsors_listing") or "").strip() == "True"
        or (row.get("has_funding_link") or "").strip() == "True"
        or (row.get("has_funding_json") or "").strip() == "True"
        or (row.get("org_fundable") or "").strip() == "True"
        or (row.get("has_npm_funding") or "").strip() == "True"
        or (row.get("has_pypi_funding") or "").strip() == "True"
        or bool((row.get("oc_slug") or "").strip())
        or bool((row.get("host") or "").strip())
        or bool((row.get("owner") or "").strip())
    )


def _fmt_score(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:g}"


def _fmt_money(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:.2f}"


def _platform_set(csv_value: str) -> set[str]:
    return {p.strip() for p in (csv_value or "").split(",") if p.strip()}


def _declares_unmeasured_channel(row: dict) -> bool:
    """True if the repo declares a funding channel whose $ we don't measure.

    A registry channel (npm `funding` / PyPI `project_urls`), a fundable
    owner/org (`org_fundable`), or a funding **link** to any platform other than
    GitHub Sponsors / Open Collective (those two are measured in dollars
    elsewhere). Such a repo has set up *a way* to be funded → not maximally
    unfunded, so its score is capped at DECLARED_FUNDING_CAP.
    """
    return (
        row.get("has_npm_funding") == "True"
        or row.get("has_pypi_funding") == "True"
        or row.get("org_fundable") == "True"
        or bool(_platform_set(row.get("funding_link_platforms")) - MEASURED_PLATFORMS)
    )


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
                 host: str, host_type: str, owner: str, owner_type: str,
                 repo_meta: dict, sponsoring_count: str = "",
                 oc_slug: str = "", oc_avg: str = "0",
                 npm_funding: dict | None = None,
                 pypi_funding: dict | None = None,
                 org_export: dict | None = None) -> dict:
    """Join one repo's raw funding signals (percentiles filled later in build()).

    `oc_slug` / `oc_avg` are the Open Collective attribution resolved in build():
    a repo-level collective's full budget, or a class-A repo's equal share of its
    org's collective (see build()). `npm_funding` / `pypi_funding` are the registry
    funding declarations (npm package.json `funding` field / PyPI `project_urls`) —
    extra declared funding channels. `export` is the repo's OWN FLOSS Fund manifest;
    `org_export` is an ORG-level manifest (registered for the whole GitHub org), so
    its channels apply to every repo the org owns — `org_fundable` flags those.
    """
    npm_funding = npm_funding or {}
    pypi_funding = pypi_funding or {}
    org_export = org_export or {}
    has_npm = (npm_funding.get("has_npm_funding") or "").strip() == "True"
    has_pypi = (pypi_funding.get("has_pypi_funding") or "").strip() == "True"
    channels = (_platform_set(yml.get("funding_link_platforms"))
                | _platform_set(export.get("channel_platforms"))
                | _platform_set(org_export.get("channel_platforms")))
    if has_npm:
        channels = channels | {"npm"}
    if has_pypi:
        channels = channels | {"pypi"}
    gh_in = (sponsors.get("github_sponsors") or "").strip()
    gh_out = (sponsoring_count or "").strip()
    row = {
        "repo": repo,
        "repo_id": repo_id,
        "gh_sponsors_in": gh_in,
        "gh_sponsors_out": gh_out,
        "owner_has_sponsors_listing": (sponsors.get("owner_has_sponsors_listing") or "").strip(),
        "gh_sponsorships": str(_to_int(gh_in) + _to_int(gh_out)),
        "gh_stars": (repo_meta.get("stars") or "").strip(),
        "gh_forks": (repo_meta.get("forks") or "").strip(),
        "has_funding_link": (yml.get("has_funding_link") or "").strip(),
        "funding_link_platforms": (yml.get("funding_link_platforms") or "").strip(),
        "has_funding_json": "True" if export else "False",
        "org_fundable": "True" if org_export else "False",
        "has_npm_funding": "True" if has_npm else "False",
        "npm_funding_url": (npm_funding.get("npm_funding_url") or "").strip(),
        "has_pypi_funding": "True" if has_pypi else "False",
        "pypi_funding_platforms": (pypi_funding.get("pypi_funding_platforms") or "").strip(),
        "channels_count": str(len(channels)),
        "oc_slug": oc_slug,
        "oc_avg_funding": oc_avg,
        "host": host,
        "host_type": host_type,
        "owner": owner,
        "owner_type": owner_type,
        "host_score": _fmt_score(_backing_score(host_type, owner_type)),
    }
    row["intent"] = "True" if _intent_flag(row) else "False"
    row["nonprofit"] = "True" if _nonprofit_flag(host_type, owner_type) else "False"
    return row


def _export_by_repo(path: Path) -> dict[str, dict]:
    """{owner/repo: manifest row} for repo-level FLOSS manifests (resolved-or-raw)."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = export_repo_slug(row)
            if repo:
                out[repo] = row
    return out


def _fundable_orgs(path: Path) -> dict[str, dict]:
    """{org_login: {channel_platforms}} for ORG-level FLOSS manifests.

    A manifest whose repo URL is a GitHub org page (`github.com/<org>`, not a
    specific repo) funds the whole org, so every repo it owns is fundable. An
    org's `channel_platforms` are unioned across all its manifests.
    """
    accum: dict[str, set[str]] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                org = github_org_page(row.get("project_repository"))
                if org:
                    accum.setdefault(org, set()).update(
                        _platform_set(row.get("channel_platforms")))
    return {org: {"channel_platforms": ",".join(sorted(p))} for org, p in accum.items()}


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


def _load_funding_overrides(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Curated institutional backing → ``(by_repo, by_org)`` from overrides.csv.

    Schema: ``repo, host, host_type, gh_user, owner, owner_type, oc_slug``.
    `host` and `owner` are **domains** (the domain is the canonical name — no
    separate `*_domain` column); `gh_user` is the GitHub login (informational);
    `oc_slug` is a curated Open Collective slug for projects that fund via OC but
    declare no FUNDING.yml (e.g. socketio).

    Two row shapes share the file:

    - **Per-repo** rows (``owner/name``) → `by_repo`, keyed by the exact slug.
    - **Org-level** rows whose `repo` is an ``owner/*`` glob → `by_org`, keyed by
      the owner login. They apply to **every** risk-scope repo under that owner
      that has no per-repo row — so one row covers a whole corporate org (e.g.
      ``boto/* → amazon.com``, ``npm/* → github.com``) instead of N near-identical
      rows. A per-repo row always wins over an org row (see build()).

    Curated per-repo/per-org institutional backing — the foundation/company that
    hosts a project and the entity that owns it. **Only a *legally connected* host
    counts**: the foundation must legally steward the project (e.g. the Apache
    Software Foundation legally holds Apache projects, the Rust Foundation holds
    the rust-lang org) — a loose marketing/community association is not a host. A
    company that legally owns the project is recorded as the `owner` instead. An
    ``owner/*`` row is only valid when the *whole* org is uniformly backed.
    """
    by_repo: dict[str, dict] = {}
    by_org: dict[str, dict] = {}
    if not path.exists():
        return by_repo, by_org
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip().lower()
            if not repo:
                continue
            rec = {
                "host": (row.get("host") or "").strip(),
                "host_type": (row.get("host_type") or "").strip(),
                "owner": (row.get("owner") or "").strip(),
                "owner_type": (row.get("owner_type") or "").strip(),
                "oc_slug": (row.get("oc_slug") or "").strip(),
            }
            if repo.endswith("/*"):
                by_org[repo[:-2]] = rec  # "boto/*" → key "boto"
            else:
                by_repo[repo] = rec
    return by_repo, by_org


def build() -> list[dict]:
    eligible = load_top_repos()
    sponsors = load_rows_by_repo(SPONSORS_FILE)
    yml = load_rows_by_repo(FUNDING_YML_FILE)
    repos_meta = load_rows_by_repo(REPOS_FILE)
    foundations = load_column_by_repo(FOUNDATIONS_FILE, "host")
    overrides, org_overrides = _load_funding_overrides(OVERRIDES_FILE)
    export = _export_by_repo(FLOSS_FUND_FILE)
    fundable_orgs = _fundable_orgs(FLOSS_FUND_FILE)
    oc_budgets = _load_oc(OC_BUDGETS_FILE)
    sponsoring = _load_sponsoring(SPONSORSHIPS_FILE)
    npm_funding = load_rows_by_repo(NPM_FUNDING_FILE) if NPM_FUNDING_FILE.exists() else {}
    pypi_funding = load_rows_by_repo(PYPI_FUNDING_FILE) if PYPI_FUNDING_FILE.exists() else {}

    # Open Collective attribution is driven by the reverse-map (collectives.csv),
    # which records whether each collective's GitHub link names a specific repo or
    # just an org — the authoritative connection.
    oc_by_repo, oc_by_org = _load_oc_index()

    def _avg(slug: str) -> float:
        try:
            return float(oc_avg_funding(slug, oc_budgets))
        except (TypeError, ValueError):
            return 0.0

    def _real_oc(slug: str) -> bool:
        """A real, existing collective — successfully fetched with `oc_status == ok`
        (as opposed to a not-found/error slug). A real OC channel is a
        sustainability-intent signal even when $0 has been raised; the dollar
        amount is carried separately by `_avg`."""
        row = oc_budgets.get(slug)
        return bool(row) and (row.get("oc_status") or "").strip() == "ok"

    # Org-budget split denominator: the org's top (class-A, valid) repos.
    classA_per_org = Counter(e.repo.split("/", 1)[0].lower()
                             for e in eligible if e.value_class == "A")

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        owner_login = repo.split("/", 1)[0].lower()
        # Per-repo override wins; else fall back to an org-level (owner/*) row.
        ov = overrides.get(repo.lower()) or org_overrides.get(owner_login) or {}
        # host: override wins; otherwise the scraped FOSS-foundation host, which
        # is a nonprofit by definition. owner: from the override only.
        scraped_host = foundations.get(repo, "")
        host = ov.get("host") or scraped_host
        host_type = ov.get("host_type") or ("nonprofit" if scraped_host else "")

        # OC budget. A curated override row is AUTHORITATIVE: its `oc_slug` wins
        # over the auto-map, including when EMPTY — an empty oc_slug on a curated
        # repo means "no OC", suppressing a spurious reverse-map match (e.g. a junk
        # collective claiming github.com/facebook). Otherwise auto-map: a repo-level
        # collective → its FULL avg budget; else the org's collective split equally
        # across the org's class-A repos.
        # A repo-level reverse-map match (the collective declares THIS owner/repo)
        # sets the slug when the collective is REAL (fetched, status ok) even at $0
        # raised — a real OC channel is itself a sustainability-intent signal;
        # `oc_amt` still carries the (possibly $0) budget. Org-level matches stay
        # guarded by `_avg(...) > 0`: an org-only $0 collective is usually junk
        # (e.g. `for-the-mage` claiming github.com/facebook), so we don't spread its
        # slug across the org's repos.
        if repo.lower() in overrides:
            s = normalize_oc_slug(ov.get("oc_slug"))
            oc_slug, oc_amt = (s, _avg(s)) if s else ("", 0.0)
        elif oc_by_repo.get(repo.lower()) and _real_oc(oc_by_repo[repo.lower()]):
            oc_slug = oc_by_repo[repo.lower()]
            oc_amt = _avg(oc_slug)
        elif (oc_by_org.get(owner_login) and entry.value_class == "A"
              and _real_oc(oc_by_org[owner_login])):
            org_slug = oc_by_org[owner_login]
            n = classA_per_org[owner_login] or 1
            oc_slug, oc_amt = org_slug, _avg(org_slug) / n
        else:
            oc_slug, oc_amt = "", 0.0

        rows.append(assemble_row(
            repo=repo, repo_id=entry.repo_id,
            sponsors=sponsors.get(repo, {}), yml=yml.get(repo, {}),
            export=export.get(repo.lower(), {}),
            host=host, host_type=host_type,
            owner=ov.get("owner", ""), owner_type=ov.get("owner_type", ""),
            repo_meta=repos_meta.get(repo, {}),
            sponsoring_count=sponsoring.get(owner_login, ""),
            oc_slug=oc_slug, oc_avg=_fmt_money(oc_amt),
            npm_funding=npm_funding.get(repo, {}),
            pypi_funding=pypi_funding.get(repo, {}),
            org_export=fundable_orgs.get(owner_login, {})))

    # Funding risk score: both axes are `lower_is_worse` (less funding → riskier).
    # add_percentiles writes the two risk percentiles; we then recompute `score`
    # as their geometric mean × the backing factor (single round, auditable).
    add_percentiles(
        rows,
        pctl_specs=[("gh_sponsorships", False), ("oc_avg_funding", False)],
        composite_cols=["gh_sponsorships_p", "oc_avg_funding_p"],
        dim_col="score",
    )
    # Funding score = geometric mean of THREE risk axes: the two channel
    # percentiles plus the combined backing (`host_score`, the most-funded of
    # host/owner). host_score (0/0.5/1) enters scaled to the 0-100 axis — company
    # 0 · nonprofit 50 · none 100 — so it's commensurate with the percentiles. A
    # company backer (0) zeroes the product → score floors at 1; a nonprofit
    # foundation (50) is one of three equal voices; no backer (100) is neutral.
    for r in rows:
        ps = [float(r[c]) for c in ("gh_sponsorships_p", "oc_avg_funding_p")
              if str(r.get(c, "")).strip()]
        if len(ps) == 2:
            backing_p = float(r["host_score"]) * 100.0
            score = max(1, round(geometric_mean(ps + [backing_p])))
            # A declared but unmeasured funding channel caps the risk: a project
            # (or its org) that has set up a way to be funded is not maximally
            # unfunded — a registry channel (npm/PyPI), a fundable owner/org, or
            # a funding link to a platform we can't value (liberapay, ko_fi, …).
            if _declares_unmeasured_channel(r):
                score = min(score, DECLARED_FUNDING_CAP)
            r["score"] = score
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
    for col in ("gh_sponsorships", "gh_stars", "has_funding_link", "has_funding_json",
                "org_fundable", "channels_count", "oc_avg_funding", "host", "owner", "score"):
        n = sum(1 for r in rows if str(r.get(col, "")) not in ("", "0", "False"))
        table.add_row(col, f"{n:,}")
    console.print(table)

    # Backing breakdown: how many repos each host/owner type discounts.
    btable = Table(title="\n[bold]Backing (host / owner)[/bold]", show_header=True,
                   header_style="bold dim", padding=(0, 1))
    btable.add_column("Field", style="bold")
    for t in ("company", "nonprofit"):
        btable.add_column(t, justify="right")
    for field in ("host_type", "owner_type"):
        c = Counter(r[field] for r in rows if r[field])
        btable.add_row(field, *(f"{c.get(t, 0):,}" for t in ("company", "nonprofit")))
    console.print(btable)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
