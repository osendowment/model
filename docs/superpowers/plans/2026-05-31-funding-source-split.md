# Funding Source Split & funding_class Removal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the raw funding signals out of `data/risk/funding-data.csv` into their source folders (`data/sources/github/`, `data/sources/floss-fund/`), derive `has_funding_json` from the FLOSS Fund directory export, delete `funding_class`, and leave only the aggregated `funding.csv` under `data/risk/`.

**Architecture:** Two new per-source GitHub fetchers (sponsors, FUNDING.yml) write to `data/sources/github/`; the existing FLOSS Fund collector moves to `src/floss_fund/` and its export is matched (by normalized repo URL) to derive `has_funding_json`. `build_funding.py` joins these sources + foundations into `data/risk/funding.csv`. A one-time migration splits the existing `funding-data.csv` so no GitHub re-query is needed.

**Tech Stack:** Python 3, `uv`, `aiohttp`, `pyyaml`, `rich`, `pytest` + `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-05-31-funding-source-split-design.md`

---

## File map

- Create: `src/floss_fund/__init__.py`, `src/floss_fund/funding_json.py` (moved), `src/floss_fund/directory.py`
- Create: `src/github/fetch_funding_yml.py`, `src/github/fetch_sponsors.py`
- Create: `scripts/migrate-funding-data.py`
- Create: `data/sources/github/sponsors.csv`, `data/sources/github/funding-yml.csv` (by migration)
- Modify: `src/pipeline/risk/build_funding.py`, `src/pipeline/common/params.py`, `src/pipeline/settings.json`, `src/pipeline/run_risk_pipeline.py`, `CLAUDE.md`
- Regenerate: `data/risk/funding.csv`, `data/risk/risk.csv`
- Delete: `src/github/fetch_funding.py`, `src/funding/` (whole dir), `data/risk/funding-data.csv`
- Tests: `tests/test_floss_fund_directory.py`, `tests/test_fetch_funding_yml.py`, `tests/test_fetch_sponsors.py`, `tests/test_migrate_funding_data.py`, `tests/test_build_funding.py`

---

## Task 1: Relocate the FLOSS Fund collector to `src/floss_fund/`

**Files:**
- Move: `src/funding/funding_json.py` → `src/floss_fund/funding_json.py`
- Create: `src/floss_fund/__init__.py` (empty)
- Delete: `src/funding/__init__.py` and the now-empty `src/funding/` dir

- [ ] **Step 1: Move the module and create the package marker**

```bash
mkdir -p src/floss_fund
git mv src/funding/funding_json.py src/floss_fund/funding_json.py
git rm src/funding/__init__.py
touch src/floss_fund/__init__.py
git add src/floss_fund/__init__.py
rmdir src/funding 2>/dev/null || true
```

- [ ] **Step 2: Update the usage docstring inside the moved file**

In `src/floss_fund/funding_json.py`, replace both usage lines:
```
    python -m src.funding.funding_json
    python -m src.funding.funding_json --ttl 0   # force refresh
```
with:
```
    python -m src.floss_fund.funding_json
    python -m src.floss_fund.funding_json --ttl 0   # force refresh
```
(The `OUTPUT_FILE = "data/sources/floss-fund/funding-json.csv"` path is unchanged.)

- [ ] **Step 3: Verify the module still imports and runs from its cache**

Run: `uv run python -m src.floss_fund.funding_json --ttl 999`
Expected: prints the cached FLOSS Fund table (no download, no traceback).

- [ ] **Step 4: Confirm no stale references to the old path**

Run: `grep -rn "src\.funding\|src/funding" src/ scripts/ tests/`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add -A src/floss_fund src/funding
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor: move FLOSS Fund collector to src/floss_fund"
```

---

## Task 2: FLOSS Fund directory loader (`src/floss_fund/directory.py`)

Pure helper that turns the export's `project_repository` URLs into a set of normalized `owner/repo` slugs, used to derive `has_funding_json`.

**Files:**
- Create: `src/floss_fund/directory.py`
- Test: `tests/test_floss_fund_directory.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_floss_fund_directory.py
from src.floss_fund.directory import normalize_github_repo, load_directory_repos


def test_normalize_basic():
    assert normalize_github_repo("https://github.com/vuejs/core") == "vuejs/core"


def test_normalize_strips_git_and_trailing_slash():
    assert normalize_github_repo("https://github.com/Eugeny/russh.git/") == "eugeny/russh"


def test_normalize_deep_path_keeps_owner_repo():
    assert normalize_github_repo("https://github.com/owner/repo/tree/main") == "owner/repo"


def test_normalize_non_github_or_blank_is_none():
    assert normalize_github_repo("https://gitlab.com/foo/bar") is None
    assert normalize_github_repo("") is None
    assert normalize_github_repo(None) is None


def test_load_directory_repos(tmp_path):
    p = tmp_path / "funding-json.csv"
    p.write_text(
        "project_repository\n"
        "https://github.com/vuejs/core\n"
        "\n"
        "https://gitlab.com/x/y\n"
        "https://github.com/Owner/Repo.git\n",
        encoding="utf-8",
    )
    assert load_directory_repos(p) == {"vuejs/core", "owner/repo"}


def test_load_directory_repos_missing_file(tmp_path):
    assert load_directory_repos(tmp_path / "nope.csv") == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_floss_fund_directory.py -v`
Expected: FAIL — `ModuleNotFoundError: src.floss_fund.directory`.

- [ ] **Step 3: Implement the loader**

```python
# src/floss_fund/directory.py
"""Match repos against the FLOSS Fund directory export.

The export (`data/sources/floss-fund/funding-json.csv`, produced by
`src.floss_fund.funding_json`) lists every registered manifest with a
`project_repository` URL. A risk-scope repo "has funding.json" iff its
`owner/repo` appears here — derived, no per-repo fetch.
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path

_GH_RE = re.compile(r"github\.com[/:]+([^/]+/[^/]+)")


def normalize_github_repo(url: str | None) -> str | None:
    """`https://github.com/Owner/Repo.git/` → `owner/repo`; non-github → None."""
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    m = _GH_RE.search(u)
    return m.group(1) if m else None


def load_directory_repos(path: Path | str) -> set[str]:
    """Set of normalized `owner/repo` slugs from the export's `project_repository`."""
    out: set[str] = set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = normalize_github_repo(row.get("project_repository"))
            if slug:
                out.add(slug)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_floss_fund_directory.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/floss_fund/directory.py tests/test_floss_fund_directory.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat: FLOSS Fund directory loader for has_funding_json derivation"
```

---

## Task 3: FUNDING.yml fetcher (`src/github/fetch_funding_yml.py`)

Per-repo `.github/FUNDING.yml` → `data/sources/github/funding-yml.csv` with `repo, repo_id, has_funding_yml, funding_yml_platforms, funding_yml_github, fetched_at`. `funding_yml_github` carries the `github:` usernames so the sponsors fetcher can count co-maintainers.

**Files:**
- Create: `src/github/fetch_funding_yml.py`
- Test: `tests/test_fetch_funding_yml.py`

- [ ] **Step 1: Write the failing tests** (parsing + github-login extraction)

```python
# tests/test_fetch_funding_yml.py
from src.github.fetch_funding_yml import parse_funding_yml, funding_yml_github_logins


def test_parse_block_list_and_custom():
    text = "github: [alice, bob]\ncustom: https://example.com/donate\npatreon:\n"
    parsed = parse_funding_yml(text)
    assert parsed["github"] == ["alice", "bob"]
    assert parsed["custom"] == "https://example.com/donate"
    assert "patreon" not in parsed  # empty value dropped


def test_parse_non_mapping_is_empty():
    assert parse_funding_yml("- just\n- a\n- list\n") == {}
    assert parse_funding_yml(": : bad yaml :::\n") == {}


def test_github_logins_from_scalar_and_list():
    assert funding_yml_github_logins({"github": "Alice"}) == ["alice"]
    assert funding_yml_github_logins({"github": ["Alice", "bob"]}) == ["alice", "bob"]
    assert funding_yml_github_logins({"patreon": "x"}) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fetch_funding_yml.py -v`
Expected: FAIL — `ModuleNotFoundError: src.github.fetch_funding_yml`.

- [ ] **Step 3: Implement the fetcher**

Create `src/github/fetch_funding_yml.py`. Port `parse_funding_yml` **verbatim** from `src/github/fetch_funding.py:108-139` (the YAML parser). Add the new `funding_yml_github_logins` helper and the standalone fetch/write/main below.

```python
"""Fetch .github/FUNDING.yml signals per risk-scope repo.

Writes data/sources/github/funding-yml.csv:
    repo, repo_id, has_funding_yml, funding_yml_platforms,
    funding_yml_github, fetched_at

`funding_yml_platforms` = the FUNDING.yml mapping keys (github, patreon, …).
`funding_yml_github`    = usernames under the `github:` key (so the sponsors
                          fetcher can count co-maintainer sponsorships).

TTL-controlled: re-runs only fetch repos missing or older than TTL_DAYS.

Usage:
    uv run python -m src.github.fetch_funding_yml
    uv run python -m src.github.fetch_funding_yml --limit 20
    uv run python -m src.github.fetch_funding_yml --force
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import datetime
import logging
import time
from pathlib import Path

import aiohttp
import yaml
from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn)

from src.github.github_client import GITHUB_API, _AsyncRateLimiter, _Deferred
from src.pipeline.common.repos import load_risk_repos

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
FIELDS = ["repo", "repo_id", "has_funding_yml", "funding_yml_platforms",
          "funding_yml_github", "fetched_at"]
TTL_DAYS = 90


def parse_funding_yml(text: str) -> dict:
    # <<< PORT VERBATIM from src/github/fetch_funding.py:108-139 >>>
    ...


def funding_yml_github_logins(yml: dict) -> list[str]:
    """Lower-cased usernames under the FUNDING.yml `github:` key (deduped, ordered)."""
    g = yml.get("github") if isinstance(yml, dict) else None
    raw = g if isinstance(g, list) else ([g] if isinstance(g, str) else [])
    seen, out = set(), []
    for u in raw:
        ul = str(u).strip().lower()
        if ul and ul not in seen:
            seen.add(ul)
            out.append(ul)
    return out


async def fetch_funding_yml(session, limiter, repo: str) -> tuple[bool, dict]:
    """Return (exists, parsed_dict). parsed_dict empty if not present."""
    url = f"{GITHUB_API}/repos/{repo}/contents/.github/FUNDING.yml"
    try:
        resp = await limiter.get(session, url)
    except _Deferred:
        return False, {}
    async with resp:
        if resp.status != 200:
            return False, {}
        try:
            payload = await resp.json()
            text = base64.b64decode(payload.get("content", "")).decode("utf-8", errors="replace")
        except Exception:
            return True, {}
        return True, parse_funding_yml(text)


async def fetch_one(session, limiter, repo: str) -> dict:
    has_yml, yml = await fetch_funding_yml(session, limiter, repo)
    platforms = list(yml.keys()) if has_yml else []
    return {
        "repo": repo,
        "has_funding_yml": "True" if has_yml else "False",
        "funding_yml_platforms": ",".join(platforms),
        "funding_yml_github": ",".join(funding_yml_github_logins(yml)),
    }


def _load_existing() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["repo"]] = row
    return out


def _load_repo_id_map() -> dict[str, str]:
    out: dict[str, str] = {}
    if GH_REPOS_FILE.exists():
        with open(GH_REPOS_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = (row.get("repo") or "").strip().lower()
                rid = (row.get("repo_id") or "").strip()
                if slug and rid:
                    out[slug] = rid
    return out


def _write(rows: dict[str, dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])


def _is_fresh(row: dict, ttl_days: int) -> bool:
    ts = (row.get("fetched_at") or "").strip()
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt >= datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)


async def batch(repos: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    repo_ids = _load_repo_id_map()
    fresh = set() if force else {r for r, row in existing.items() if _is_fresh(row, TTL_DAYS)}
    to_fetch = [r for r in repos if r not in fresh]
    if limit and limit < len(to_fetch):
        import random
        to_fetch = random.sample(to_fetch, limit)
    console.print(f"[bold]funding-yml[/bold]: {len(repos)} repos, {len(to_fetch)} to fetch")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return
    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(repo: str) -> dict:
        async with sem:
            try:
                return await fetch_one(session, limiter, repo)
            except RuntimeError as e:
                log.warning("funding-yml failed for %s: %s", repo, e)
                return {"repo": repo, "_error": str(e)}

    headers = {"Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("funding-yml", total=len(to_fetch))
            for coro in asyncio.as_completed([one(r) for r in to_fetch]):
                res = await coro
                prog.advance(task)
                if "_error" in res:
                    continue
                res["repo_id"] = repo_ids.get(res["repo"], "")
                res["fetched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                existing[res["repo"]] = res
                _write(existing)
    console.print(f"[green]done[/green] → {OUTPUT_FILE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    repos = sorted({e.repo for e in load_risk_repos() if e.repo})
    asyncio.run(batch(repos, args.force, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run unit tests to verify they pass**

Run: `uv run pytest tests/test_fetch_funding_yml.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Smoke-test against live GitHub with a tiny limit** (NOT a full run)

Run: `uv run python -m src.github.fetch_funding_yml --limit 3`
Expected: writes 3 rows to `data/sources/github/funding-yml.csv` without traceback. (Full fetch is deferred — real data comes from the Task 5 migration.)

- [ ] **Step 6: Discard the 3-row smoke output so migration owns the file**

Run: `git checkout -- data/sources/github/funding-yml.csv 2>/dev/null || rm -f data/sources/github/funding-yml.csv`

- [ ] **Step 7: Commit**

```bash
git add src/github/fetch_funding_yml.py tests/test_fetch_funding_yml.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat: standalone FUNDING.yml fetcher -> data/sources/github/funding-yml.csv"
```

---

## Task 4: Sponsors fetcher (`src/github/fetch_sponsors.py`)

Per-repo GitHub Sponsors count → `data/sources/github/sponsors.csv` with `repo, repo_id, github_sponsors, sponsors_status, fetched_at`. Queries the repo owner plus any `github:` logins from `funding-yml.csv`. `sponsors_status` ∈ `{ok, error}` so a `0` from a failed query is distinguishable from a genuine `0`.

**Files:**
- Create: `src/github/fetch_sponsors.py`
- Test: `tests/test_fetch_sponsors.py`

- [ ] **Step 1: Write the failing tests** (login set + status logic)

```python
# tests/test_fetch_sponsors.py
from src.github.fetch_sponsors import logins_for_repo, status_from_counts


def test_logins_owner_plus_yml(tmp_path):
    yml = {"owner/repo": "alice,bob"}  # funding_yml_github map
    assert logins_for_repo("owner/repo", yml) == ["owner", "alice", "bob"]


def test_logins_owner_only_when_no_yml():
    assert logins_for_repo("owner/repo", {}) == ["owner"]


def test_logins_dedupe_owner_in_yml():
    yml = {"owner/repo": "owner,carol"}
    assert logins_for_repo("owner/repo", yml) == ["owner", "carol"]


def test_status_ok_vs_error():
    assert status_from_counts([0, 3], any_error=False) == "ok"
    assert status_from_counts([0], any_error=True) == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fetch_sponsors.py -v`
Expected: FAIL — `ModuleNotFoundError: src.github.fetch_sponsors`.

- [ ] **Step 3: Implement the fetcher**

Create `src/github/fetch_sponsors.py`. Port `SPONSORS_QUERY` (`fetch_funding.py:90-103`) and `fetch_sponsors_for_login` (`fetch_funding.py:245-263`) **verbatim**, but change `fetch_sponsors_for_login` to return `tuple[int, bool]` `(count, ok)` — `ok=False` on `_Deferred`/`RuntimeError` instead of swallowing to `0`.

```python
"""Fetch GitHub Sponsors counts per risk-scope repo.

Writes data/sources/github/sponsors.csv:
    repo, repo_id, github_sponsors, sponsors_status, fetched_at

Counts public sponsorships of the repo owner plus every `github:` login in
the repo's FUNDING.yml (read from data/sources/github/funding-yml.csv — run
fetch_funding_yml first). `sponsors_status`: "ok" if every queried login
resolved, "error" if any GraphQL query failed (so a 0 from a failure is not
mistaken for a genuine 0).

Usage:
    uv run python -m src.github.fetch_sponsors
    uv run python -m src.github.fetch_sponsors --limit 20
    uv run python -m src.github.fetch_sponsors --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn)

from src.github.github_client import _AsyncRateLimiter, _Deferred, _graphql
from src.pipeline.common.repos import load_risk_repos

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
FIELDS = ["repo", "repo_id", "github_sponsors", "sponsors_status", "fetched_at"]
TTL_DAYS = 90

SPONSORS_QUERY = """
query($login: String!) {
  user(login: $login) { sponsorshipsAsMaintainer(first: 1, includePrivate: false) { totalCount } }
  organization(login: $login) { sponsorshipsAsMaintainer(first: 1, includePrivate: false) { totalCount } }
}
"""


def logins_for_repo(repo: str, yml_github: dict[str, str]) -> list[str]:
    """Owner login + FUNDING.yml `github:` logins (deduped, owner first)."""
    owner = repo.split("/", 1)[0].lower()
    extra = [u.strip().lower() for u in (yml_github.get(repo, "") or "").split(",") if u.strip()]
    seen, out = set(), []
    for u in [owner, *extra]:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def status_from_counts(counts: list[int], any_error: bool) -> str:
    """"error" if any queried login failed, else "ok"."""
    return "error" if any_error else "ok"


async def fetch_sponsors_for_login(session, limiter, login: str) -> tuple[int, bool]:
    """(public sponsor count, ok). ok=False on a failed/deferred query."""
    if not login:
        return 0, True
    try:
        result = await _graphql(session, limiter, SPONSORS_QUERY, {"login": login})
    except _Deferred:
        return 0, False
    except RuntimeError as e:
        log.warning("graphql failed for %s: %s", login, e)
        return 0, False
    data = result.get("data") or {}
    user = (data.get("user") or {}).get("sponsorshipsAsMaintainer") or {}
    org = (data.get("organization") or {}).get("sponsorshipsAsMaintainer") or {}
    return int(user.get("totalCount") or 0) + int(org.get("totalCount") or 0), True


async def fetch_one(session, limiter, repo: str, yml_github: dict[str, str]) -> dict:
    logins = logins_for_repo(repo, yml_github)
    results = await asyncio.gather(*(fetch_sponsors_for_login(session, limiter, l) for l in logins))
    total = sum(c for c, _ in results)
    any_error = any(not ok for _, ok in results)
    return {
        "repo": repo,
        "github_sponsors": str(total),
        "sponsors_status": status_from_counts([c for c, _ in results], any_error),
    }


def _load_map(path: Path, key: str, val: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k = (row.get(key) or "").strip().lower()
                if k:
                    out[k] = (row.get(val) or "").strip()
    return out


def _load_existing() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["repo"]] = row
    return out


def _write(rows: dict[str, dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])


def _is_fresh(row: dict, ttl_days: int) -> bool:
    ts = (row.get("fetched_at") or "").strip()
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt >= datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)


async def batch(repos: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    repo_ids = _load_map(GH_REPOS_FILE, "repo", "repo_id")
    yml_github = _load_map(FUNDING_YML_FILE, "repo", "funding_yml_github")
    fresh = set() if force else {r for r, row in existing.items() if _is_fresh(row, TTL_DAYS)}
    to_fetch = [r for r in repos if r not in fresh]
    if limit and limit < len(to_fetch):
        import random
        to_fetch = random.sample(to_fetch, limit)
    console.print(f"[bold]sponsors[/bold]: {len(repos)} repos, {len(to_fetch)} to fetch")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return
    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(repo: str) -> dict:
        async with sem:
            try:
                return await fetch_one(session, limiter, repo, yml_github)
            except RuntimeError as e:
                log.warning("sponsors failed for %s: %s", repo, e)
                return {"repo": repo, "_error": str(e)}

    headers = {"Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("sponsors", total=len(to_fetch))
            for coro in asyncio.as_completed([one(r) for r in to_fetch]):
                res = await coro
                prog.advance(task)
                if "_error" in res:
                    continue
                res["repo_id"] = repo_ids.get(res["repo"], "")
                res["fetched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                existing[res["repo"]] = res
                _write(existing)
    console.print(f"[green]done[/green] → {OUTPUT_FILE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    repos = sorted({e.repo for e in load_risk_repos() if e.repo})
    asyncio.run(batch(repos, args.force, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run unit tests to verify they pass**

Run: `uv run pytest tests/test_fetch_sponsors.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Smoke-test against live GitHub with a tiny limit** (NOT a full run)

Run: `uv run python -m src.github.fetch_sponsors --limit 3`
Expected: writes 3 rows to `data/sources/github/sponsors.csv` without traceback.

- [ ] **Step 6: Discard the smoke output so migration owns the file**

Run: `git checkout -- data/sources/github/sponsors.csv 2>/dev/null || rm -f data/sources/github/sponsors.csv`

- [ ] **Step 7: Commit**

```bash
git add src/github/fetch_sponsors.py tests/test_fetch_sponsors.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat: standalone sponsors fetcher with sponsors_status -> data/sources/github/sponsors.csv"
```

---

## Task 5: One-time migration (`scripts/migrate-funding-data.py`)

Split the existing `data/risk/funding-data.csv` into `sponsors.csv` + `funding-yml.csv` (preserve values + timestamps, no GitHub re-query), then remove `funding-data.csv`. `funding_yml_github` is left empty (repopulated on next real fetch); `has_funding_json`/`funding_5y`/`funding_class`/`funding_sources` are dropped.

**Files:**
- Create: `scripts/migrate-funding-data.py`
- Test: `tests/test_migrate_funding_data.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_funding_data.py
import csv
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "migrate_funding_data", Path("scripts/migrate-funding-data.py"))
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def test_split_rows():
    rows = [{
        "repo": "owner/repo", "repo_id": "42", "github_sponsors": "5",
        "has_funding_yml": "True", "funding_yml_platforms": "github,patreon",
        "has_funding_json": "False", "funding_5y": "", "funding_sources": "2",
        "funding_class": "C", "fetched_at": "2026-05-19T11:10:36+00:00",
    }]
    sponsors, yml = mig.split_rows(rows)
    assert sponsors[0] == {
        "repo": "owner/repo", "repo_id": "42", "github_sponsors": "5",
        "sponsors_status": "ok", "fetched_at": "2026-05-19T11:10:36+00:00"}
    assert yml[0] == {
        "repo": "owner/repo", "repo_id": "42", "has_funding_yml": "True",
        "funding_yml_platforms": "github,patreon", "funding_yml_github": "",
        "fetched_at": "2026-05-19T11:10:36+00:00"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_funding_data.py -v`
Expected: FAIL — `FileNotFoundError`/`AttributeError: split_rows`.

- [ ] **Step 3: Implement the migration script**

```python
# scripts/migrate-funding-data.py
"""One-time: split data/risk/funding-data.csv into source-folder CSVs.

    data/risk/funding-data.csv
      → data/sources/github/sponsors.csv     (repo, repo_id, github_sponsors,
                                               sponsors_status, fetched_at)
      → data/sources/github/funding-yml.csv  (repo, repo_id, has_funding_yml,
                                               funding_yml_platforms,
                                               funding_yml_github, fetched_at)

Preserves fetched values + timestamps (no GitHub re-query). `funding_yml_github`
is left empty (repopulated on next real fetch). has_funding_json / funding_5y /
funding_sources / funding_class are dropped (has_funding_json is derived from
the FLOSS Fund export at build time). Then removes funding-data.csv.

Usage:
    uv run python scripts/migrate-funding-data.py
"""
from __future__ import annotations

import csv
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()
DATA = Path("data")
SRC = DATA / "risk" / "funding-data.csv"
SPONSORS = DATA / "sources" / "github" / "sponsors.csv"
YML = DATA / "sources" / "github" / "funding-yml.csv"

SPONSORS_FIELDS = ["repo", "repo_id", "github_sponsors", "sponsors_status", "fetched_at"]
YML_FIELDS = ["repo", "repo_id", "has_funding_yml", "funding_yml_platforms",
              "funding_yml_github", "fetched_at"]


def split_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    sponsors, yml = [], []
    for r in rows:
        sponsors.append({
            "repo": r.get("repo", ""), "repo_id": r.get("repo_id", ""),
            "github_sponsors": r.get("github_sponsors", ""),
            "sponsors_status": "ok", "fetched_at": r.get("fetched_at", "")})
        yml.append({
            "repo": r.get("repo", ""), "repo_id": r.get("repo_id", ""),
            "has_funding_yml": r.get("has_funding_yml", ""),
            "funding_yml_platforms": r.get("funding_yml_platforms", ""),
            "funding_yml_github": "", "fetched_at": r.get("fetched_at", "")})
    return sponsors, yml


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["repo"]))


def main() -> None:
    if not SRC.exists():
        console.print(f"[yellow]{SRC} not found — nothing to migrate.[/yellow]")
        return
    with open(SRC, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    sponsors, yml = split_rows(rows)
    _write(SPONSORS, SPONSORS_FIELDS, sponsors)
    _write(YML, YML_FIELDS, yml)
    console.print(f"[green]Wrote[/green] {len(sponsors)} → {SPONSORS}")
    console.print(f"[green]Wrote[/green] {len(yml)} → {YML}")
    subprocess.run(["git", "rm", "-q", str(SRC)], check=False)
    console.print(f"[green]Removed[/green] {SRC}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_migrate_funding_data.py -v`
Expected: PASS.

- [ ] **Step 5: Run the migration for real**

Run: `uv run python scripts/migrate-funding-data.py`
Expected: writes ~951 rows to each of `sponsors.csv` / `funding-yml.csv`; `git rm`s `funding-data.csv`.

- [ ] **Step 6: Verify the outputs**

Run: `head -2 data/sources/github/sponsors.csv data/sources/github/funding-yml.csv; test ! -f data/risk/funding-data.csv && echo "funding-data.csv removed"`
Expected: correct headers; "funding-data.csv removed".

- [ ] **Step 7: Commit**

```bash
git add scripts/migrate-funding-data.py tests/test_migrate_funding_data.py data/sources/github/sponsors.csv data/sources/github/funding-yml.csv data/risk/funding-data.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "data: split funding-data.csv into github/ source files (migration)"
```

---

## Task 6: Rewrite `build_funding.py`

Join the new sources + foundations + FLOSS Fund export → `data/risk/funding.csv` with `repo, repo_id, github_sponsors, has_funding_yml, funding_yml_platforms, has_funding_json, foundation_host, fetched_at`. No `funding_class`, no `funding_5y`.

**Files:**
- Modify (rewrite): `src/pipeline/risk/build_funding.py`
- Test: `tests/test_build_funding.py`

- [ ] **Step 1: Write the failing test** (join + has_funding_json derivation)

```python
# tests/test_build_funding.py
from src.pipeline.risk import build_funding as bf


def test_assemble_row_joins_sources_and_derives_json():
    row = bf.assemble_row(
        repo="vuejs/core", repo_id="11730342",
        sponsors={"github_sponsors": "12", "fetched_at": "2026-05-19T10:00:00+00:00"},
        yml={"has_funding_yml": "True", "funding_yml_platforms": "github",
             "fetched_at": "2026-05-19T11:00:00+00:00"},
        foundation_host="",
        directory_repos={"vuejs/core"},
    )
    assert row == {
        "repo": "vuejs/core", "repo_id": "11730342", "github_sponsors": "12",
        "has_funding_yml": "True", "funding_yml_platforms": "github",
        "has_funding_json": "True", "foundation_host": "",
        "fetched_at": "2026-05-19T11:00:00+00:00"}  # latest of the two sources
    assert "funding_class" not in row


def test_assemble_row_absent_from_directory_is_false():
    row = bf.assemble_row(
        repo="acornjs/acorn", repo_id="1", sponsors={}, yml={},
        foundation_host="apache", directory_repos=set())
    assert row["has_funding_json"] == "False"
    assert row["github_sponsors"] == ""
    assert row["foundation_host"] == "apache"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_build_funding.py -v`
Expected: FAIL — `AttributeError: assemble_row`.

- [ ] **Step 3: Rewrite the builder**

Replace the entire contents of `src/pipeline/risk/build_funding.py` with:

```python
#!/usr/bin/env python3
"""Build data/risk/funding.csv — funding signals per risk-scope repo.

Reads (all under data/sources/):
    github/sponsors.csv        — github_sponsors  (src.github.fetch_sponsors)
    github/funding-yml.csv     — has_funding_yml / funding_yml_platforms
                                 (src.github.fetch_funding_yml)
    floss-fund/funding-json.csv — FLOSS Fund directory export; has_funding_json
                                 is derived by matching repo URLs
                                 (src.floss_fund.funding_json)
    foundations/host-by-repo.csv — FOSS-foundation host per repo

Writes data/risk/funding.csv:
    repo, repo_id, github_sponsors, has_funding_yml, funding_yml_platforms,
    has_funding_json, foundation_host, fetched_at

`has_funding_json` is True iff the repo is registered in the FLOSS Fund
directory (no per-repo fetch). `fetched_at` is the most recent of the
contributing source rows' timestamps. No funding class is computed.

Usage:
    uv run python -m src.pipeline.risk.build_funding
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.floss_fund.directory import load_directory_repos
from src.pipeline.common.repos import load_risk_repos
from src.pipeline.common.tables import load_column_by_repo, load_rows_by_repo

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
SPONSORS_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
FLOSS_FUND_FILE = DATA_DIR / "sources" / "floss-fund" / "funding-json.csv"
FOUNDATIONS_FILE = DATA_DIR / "sources" / "foundations" / "host-by-repo.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "funding.csv"

FIELDS = ["repo", "repo_id", "github_sponsors", "has_funding_yml",
          "funding_yml_platforms", "has_funding_json", "foundation_host",
          "fetched_at"]


def _latest(*timestamps: str) -> str:
    """Most recent ISO timestamp among the args (lexical order works for ISO)."""
    return max((t for t in timestamps if t), default="")


def assemble_row(repo: str, repo_id: str, sponsors: dict, yml: dict,
                 foundation_host: str, directory_repos: set) -> dict:
    """Join one repo's signals into a funding.csv row."""
    return {
        "repo": repo,
        "repo_id": repo_id,
        "github_sponsors": (sponsors.get("github_sponsors") or "").strip(),
        "has_funding_yml": (yml.get("has_funding_yml") or "").strip(),
        "funding_yml_platforms": (yml.get("funding_yml_platforms") or "").strip(),
        "has_funding_json": "True" if repo.lower() in directory_repos else "False",
        "foundation_host": foundation_host,
        "fetched_at": _latest((sponsors.get("fetched_at") or "").strip(),
                              (yml.get("fetched_at") or "").strip()),
    }


def build() -> list[dict]:
    eligible = load_risk_repos()
    sponsors = load_rows_by_repo(SPONSORS_FILE)
    yml = load_rows_by_repo(FUNDING_YML_FILE)
    foundations = load_column_by_repo(FOUNDATIONS_FILE, "host")
    directory_repos = load_directory_repos(FLOSS_FUND_FILE)

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        rows.append(assemble_row(
            repo=repo, repo_id=entry.repo_id,
            sponsors=sponsors.get(repo, {}), yml=yml.get(repo, {}),
            foundation_host=foundations.get(repo, ""),
            directory_repos=directory_repos))
    return rows


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
    for col in ("github_sponsors", "has_funding_yml", "funding_yml_platforms",
                "has_funding_json", "foundation_host"):
        n = sum(1 for r in rows if r[col] and r[col] != "False")
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_build_funding.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Regenerate funding.csv for real**

Run: `uv run python -m src.pipeline.risk.build_funding`
Expected: prints coverage table; writes ~899 rows.

- [ ] **Step 6: Verify funding_class is gone and has_funding_json is correct**

Run:
```bash
head -1 data/risk/funding.csv
uv run python -c "import csv; rows=list(csv.DictReader(open('data/risk/funding.csv'))); print('funding_class' in rows[0]); print(sorted(r['repo'] for r in rows if r['has_funding_json']=='True'))"
```
Expected: header has no `funding_class`; first line prints `False`; the True list is `['browserify/resolve', 'eemeli/yaml', 'openssl/openssl', 'vuejs/core']`.

- [ ] **Step 7: Commit**

```bash
git add src/pipeline/risk/build_funding.py tests/test_build_funding.py data/risk/funding.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat: build_funding joins source files + derives has_funding_json from FLOSS Fund export, drops funding_class"
```

---

## Task 7: Remove old collector, dead config, rewire pipeline, update docs

**Files:**
- Delete: `src/github/fetch_funding.py`
- Modify: `src/pipeline/settings.json`, `src/pipeline/common/params.py`, `src/pipeline/run_risk_pipeline.py`, `CLAUDE.md`

- [ ] **Step 1: Delete the old combined collector**

```bash
git rm src/github/fetch_funding.py
```

- [ ] **Step 2: Remove the dead `risk_classification.funding` block from settings.json**

In `src/pipeline/settings.json`, delete the `"funding": { "A": 0, "B": 10, "C": 100, "comment": "..." }` entry inside `risk_classification` (and the trailing comma on the preceding sibling, so the JSON stays valid).

- [ ] **Step 3: Remove the `FUNDING_THRESHOLDS` binding from params.py**

In `src/pipeline/common/params.py`, delete the line:
```python
FUNDING_THRESHOLDS: dict = _P["risk_classification"]["funding"]
```

- [ ] **Step 4: Rewire the risk pipeline fetchers**

In `src/pipeline/run_risk_pipeline.py`, replace the single funding fetcher line:
```python
    Step("funding",       "src.github.fetch_funding",             fetch=True),
```
with (FUNDING.yml before sponsors; export before the builder):
```python
    Step("funding-yml",   "src.github.fetch_funding_yml",         fetch=True),
    Step("sponsors",      "src.github.fetch_sponsors",            fetch=True),
    Step("floss-fund",    "src.floss_fund.funding_json",          fetch=True),
```

- [ ] **Step 5: Update the CLAUDE.md data-organization line**

In `CLAUDE.md`, in the `data/risk/` bullet, change the tail
"… and the raw `funding-data.csv` fetch." to:
"… . Raw funding signals live under `data/sources/github/` (`sponsors.csv`, `funding-yml.csv`) and `data/sources/floss-fund/`."

- [ ] **Step 6: Verify settings.json is valid and nothing imports removed symbols**

Run:
```bash
uv run python -c "import json; json.load(open('src/pipeline/settings.json')); print('settings.json OK')"
grep -rn "FUNDING_THRESHOLDS\|fetch_funding\b\|src.github.fetch_funding\b\|funding_class" src/ tests/ || echo "no stale references"
```
Expected: "settings.json OK"; "no stale references" (the bare `fetch_funding`/`funding_class` are gone — `fetch_funding_yml` is a different token and won't match `fetch_funding\b`).

- [ ] **Step 7: Commit**

```bash
git add -A src/github/fetch_funding.py src/pipeline/settings.json src/pipeline/common/params.py src/pipeline/run_risk_pipeline.py CLAUDE.md
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor: drop old funding collector + funding_class config, rewire risk pipeline fetchers"
```

---

## Task 8: Regenerate risk.csv and final verification

**Files:**
- Regenerate: `data/risk/risk.csv`

- [ ] **Step 1: Re-aggregate risk.csv from the per-dimension builds**

Run: `uv run python -m src.pipeline.risk.aggregate_risk`
Expected: writes `data/risk/risk.csv` for all risk-scope repos without error.

- [ ] **Step 2: Verify risk.csv no longer carries funding_class but keeps funding columns**

Run:
```bash
uv run python -c "import csv; r=next(csv.DictReader(open('data/risk/risk.csv'))); print('funding_class' in r, 'github_sponsors' in r, 'has_funding_json' in r)"
```
Expected: `False True True`.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass (including the four new test files).

- [ ] **Step 4: Commit the regenerated risk table**

```bash
git add data/risk/risk.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "data: regenerate risk.csv (funding_class column removed)"
```

---

## Self-review notes

- **Spec coverage:** sources split (Tasks 3,4,5 + migration), funding.json from export (Tasks 2,6), funding_class removal (Tasks 6,7,8), funding_5y drop (Tasks 5,6), module move (Task 1), pipeline rewire + docs (Task 7), regenerate (Tasks 6,8). All covered.
- **Type consistency:** `funding_yml_github` column name used identically across Tasks 3/4/5; `assemble_row`, `load_directory_repos`, `normalize_github_repo`, `split_rows`, `status_from_counts`, `logins_for_repo` signatures match their tests.
- **No full fetcher runs:** Tasks 3/4 smoke-test with `--limit 3` only; real per-repo data comes from the Task 5 migration (no GitHub re-query), honoring the no-full-run rule.
