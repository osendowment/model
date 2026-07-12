"""Fetch annual Open Collective budgets (gross raised) for funded repos.

For every Open Collective handle declared by a risk-scope repo — in
`data/sources/github/funding-yml.csv` (`open_collective` column) or the FLOSS
Fund export (`data/sources/floss-fund/funding-json.csv`) — query the public
Open Collective GraphQL API for `totalAmountReceived` (gross incoming
donations) in each calendar year 2021-2025.

Writes data/sources/opencollective/budgets.csv:
    slug, raised_2021..raised_2025, currency, oc_status, fetched_at

`oc_status`: "ok" (account resolved), "not_found" (no such collective),
"error" (request failed — distinguishes a real 0 from a fetch failure).
Amounts are major currency units (USD etc.); a year with no data is blank.

The API works unauthenticated but rate-limits hard (HTTP 429); set
`OPENCOLLECTIVE_PERSONAL_TOKEN` (in `.env`) to lift the limit — it is sent as
the `Personal-Token` header and loaded via python-dotenv. The default
user-agent is rejected, so we always send a custom one.

Usage:
    uv run python -m src.sources.opencollective.fetch_budgets
    uv run python -m src.sources.opencollective.fetch_budgets --limit 10
    uv run python -m src.sources.opencollective.fetch_budgets --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from src.common.freshness import row_is_fresh
from src.common.funding_platforms import normalize_oc_slug
from src.common.repos import load_top_repos
from src.sources.floss_fund.directory import export_repo_slug, normalize_repo_url

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "opencollective" / "budgets.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
OVERRIDES_FILE = DATA_DIR / "eligibility" / "overrides.csv"
FLOSS_FUND_FILE = DATA_DIR / "sources" / "floss-fund" / "funding-json.csv"

API_URL = "https://api.opencollective.com/graphql/v2"
USER_AGENT = "Mozilla/5.0 (research; endowment.dev funding model)"
YEARS = list(range(2021, 2026))  # 2021..2025
FIELDS = ["slug"] + [f"raised_{y}" for y in YEARS] + ["currency", "oc_status", "fetched_at"]


def _money(cents) -> str:
    """Integer-cent amount → major-unit string without spurious decimals."""
    v = cents / 100
    return str(int(v)) if v == int(v) else f"{v:.2f}"


def build_query() -> str:
    """5-year aliased query: one `totalAmountReceived` window per year."""
    fields = "\n".join(
        f'y{y}: totalAmountReceived('
        f'dateFrom: "{y}-01-01T00:00:00Z", dateTo: "{y}-12-31T23:59:59Z") '
        f"{{ valueInCents currency }}"
        for y in YEARS
    )
    return f"query($slug: String!) {{ account(slug: $slug) {{ slug name currency stats {{ {fields} }} }} }}"


def parse_oc_account(slug: str, account: dict | None) -> dict:
    """Turn a GraphQL `account` payload into a budgets.csv row (no fetched_at)."""
    if account is None:
        return {"slug": slug, "oc_status": "not_found", "currency": "",
                **{f"raised_{y}": "" for y in YEARS}}
    stats = account.get("stats") or {}
    currency = account.get("currency") or ""
    row = {"slug": slug, "oc_status": "ok"}
    for y in YEARS:
        amt = stats.get(f"y{y}") or {}
        cents = amt.get("valueInCents")
        row[f"raised_{y}"] = _money(cents) if cents is not None else ""
        if amt.get("currency"):
            currency = amt["currency"]
    row["currency"] = currency
    return row


# ── slug discovery ───────────────────────────────────────────────────────────

def _oc_slugs(cell: str) -> set[str]:
    return {s for h in (cell or "").split(",") if (s := normalize_oc_slug(h))}


def _slugs_from_yml(path: Path) -> set[str]:
    """OC slugs from funding-yml.csv (already keyed to risk-scope repos)."""
    out: set[str] = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out |= _oc_slugs(row.get("open_collective"))
    return out


def _slugs_from_export(path: Path, scope: set[str]) -> set[str]:
    """OC slugs from the FLOSS Fund export, restricted to in-scope repos.

    The export is the global directory, so we keep only manifests whose
    `project_repository` resolves to a repo in the risk scope — otherwise we'd
    fetch budgets for hundreds of collectives we don't track.
    """
    out: set[str] = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if export_repo_slug(row) in scope:
                    out |= _oc_slugs(row.get("open_collective"))
    return out


def _slugs_from_overrides(path: Path) -> set[str]:
    """OC slugs curated per-repo in data/eligibility/overrides.csv (`oc_slug` column).

    Catches projects that fund via Open Collective but declare no FUNDING.yml
    (e.g. socketio), which the registry/FLOSS-export discovery can't see.
    """
    out: set[str] = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out |= _oc_slugs(row.get("oc_slug"))
    return out


def _slugs_from_collectives(scope_repos: set[str], scope_orgs: set[str],
                            scope_gitlab_urls: set[str] = frozenset()) -> set[str]:
    """OC slugs whose GitHub link (repo or org) — or GitLab project URL — matches
    a risk-scope repo.

    The reverse map (data/sources/opencollective/collectives.csv) — OC declares
    which repo/org each collective funds, so this catches OC-funded projects that
    never declared a FUNDING.yml (socketio, …) without any guessing. GitLab-linked
    collectives join by normalized clone URL (`scope_gitlab_urls`).
    """
    from src.sources.opencollective import fetch_collectives
    by_repo, by_org = fetch_collectives.load_index()
    out = {slug for repo, slug in by_repo.items() if repo in scope_repos}
    out |= {slug for org, slug in by_org.items() if org in scope_orgs}
    out |= {slug for url, slug in fetch_collectives.load_url_index().items()
            if url in scope_gitlab_urls}
    return out


def collect_slugs() -> list[str]:
    """Distinct Open Collective slugs referenced by risk-scope repos."""
    entries = load_top_repos()
    scope = {e.repo.lower() for e in entries}
    orgs = {r.split("/", 1)[0] for r in scope}
    gitlab_urls = {normalize_repo_url(e.git_url) for e in entries
                   if str(e.repo_id).startswith("gl/") and e.git_url}
    return sorted(_slugs_from_yml(FUNDING_YML_FILE)
                  | _slugs_from_export(FLOSS_FUND_FILE, scope)
                  | _slugs_from_overrides(OVERRIDES_FILE)
                  | _slugs_from_collectives(scope, orgs, gitlab_urls))


# ── CSV I/O ──────────────────────────────────────────────────────────────────

def _load_existing() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["slug"]] = row
    return out


def _write(rows: dict[str, dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for slug in sorted(rows):
            w.writerow(rows[slug])


# ── fetch ────────────────────────────────────────────────────────────────────

def _retry_after(resp: aiohttp.ClientResponse, attempt: int) -> float:
    """Seconds to wait before retry — honour Retry-After, else capped backoff."""
    hdr = resp.headers.get("Retry-After")
    if hdr and hdr.strip().isdigit():
        return min(float(hdr), 90.0)
    return min(15.0 * (attempt + 1), 90.0)


async def fetch_one(session: aiohttp.ClientSession, query: str, slug: str,
                    max_retries: int = 6) -> dict:
    """Fetch one slug, retrying on 429 (rate limit) / transient errors."""
    payload = {"query": query, "variables": {"slug": slug}}
    for attempt in range(max_retries + 1):
        try:
            async with session.post(API_URL, json=payload, timeout=30) as resp:
                if resp.status == 429:
                    if attempt < max_retries:
                        wait = _retry_after(resp, attempt)
                        log.info("oc 429 for %s — waiting %.0fs (retry %d)", slug, wait, attempt + 1)
                        await asyncio.sleep(wait)
                        continue
                    return {"slug": slug, "oc_status": "error"}
                if resp.status != 200:
                    return {"slug": slug, "oc_status": "error"}
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < max_retries:
                await asyncio.sleep(min(5.0 * (attempt + 1), 30.0))
                continue
            log.warning("oc fetch failed for %s: %s", slug, e)
            return {"slug": slug, "oc_status": "error"}
        account = (data.get("data") or {}).get("account")
        return parse_oc_account(slug, account)
    return {"slug": slug, "oc_status": "error"}


async def batch(slugs: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    fresh = set() if force else {s for s, row in existing.items()
                                 if row_is_fresh(row, status_key="oc_status")}
    to_fetch = [s for s in slugs if s not in fresh]
    if limit and limit < len(to_fetch):
        to_fetch = to_fetch[:limit]
    console.print(f"[bold]opencollective[/bold]: {len(slugs)} slugs, {len(to_fetch)} to fetch")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return

    query = build_query()
    sem = asyncio.Semaphore(concurrency)
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    load_dotenv()
    token = os.environ.get("OPENCOLLECTIVE_PERSONAL_TOKEN") or os.environ.get("OC_PERSONAL_TOKEN")
    if token:
        headers["Personal-Token"] = token
        console.print("[dim]Using OpenCollective Personal-Token (higher rate limit).[/dim]")

    async with aiohttp.ClientSession(headers=headers) as session:
        async def one(slug: str) -> dict:
            async with sem:
                return await fetch_one(session, query, slug)

        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("opencollective", total=len(to_fetch))
            for coro in asyncio.as_completed([one(s) for s in to_fetch]):
                res = await coro
                prog.advance(task)
                res["fetched_at"] = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(timespec="seconds")
                existing[res["slug"]] = res
                _write(existing)
    console.print(f"[green]done[/green] → {OUTPUT_FILE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=2,
                   help="keep low — the unauthenticated OC API rate-limits hard")
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    slugs = collect_slugs()
    asyncio.run(batch(slugs, args.force, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
