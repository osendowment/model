# Workload Class + Contributor-Fetch Cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a percentile-based `workload_class` (A–D) to the risk pipeline, and clean up the unstable GitHub `/stats/contributors` code path it depends on.

**Architecture:** Four phases in dependency order. (1) Delete the dead `/stats/contributors` code. (2) Delete the stale per-year contributor CSVs, consolidating onto `concentration-data.csv`. (3) Add an `active_contributors` (AC) count + keep the fetch date. (4) Compute `workload_class` in `build_workload.py` from the geometric mean of Hazen percentiles of LOC/AC, CVE/AC, NNI/AC, bucketed into equal-count quartiles.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `csv`/`rich` stdlib + libs. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-18-workload-class-design.md`

**Commit author:** every `git commit` in this plan must use
`git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit ...`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/github/github_client.py` | GitHub HTTP layer | remove `/stats/contributors` fetchers |
| `src/github/fetch_contributors_metrics.py` | contributor CLI + lifetime BF/HHI | remove yearly/stats path; rewrite single-repo CLI |
| `src/github/display.py` | rich terminal output | remove `display_yearly_breakdown` |
| `src/github/batch_runner.py` | async batch fetch + CSV I/O | remove wide-file writers; add `active_contributors` |
| `src/pipeline/risk/build_concentration.py` | concentration intermediate | read `concentration-data.csv` only; emit `active_contributors` |
| `src/pipeline/risk/build_workload.py` | workload intermediate | add `workload_class` + ratios + percentiles |
| `src/pipeline/settings.json` | pipeline config | add `risk_classification.workload` |
| `docs/risk.md` | risk-pipeline docs | workload class table + roadmap/output updates |
| `data/concentration-data.csv` | contributor metrics | add `active_contributors` column (one-time migration) |
| `data/sources/github/contributors/*.csv` | stale per-year metrics | **deleted** (6 files) |
| `tests/test_contrib_metrics.py` | — | drop tests for removed functions |
| `tests/test_contributors.py` | — | drop tests for removed functions |
| `tests/test_batch.py` | — | replace yearly-CSV tests with concentration-data tests |
| `tests/test_build_workload.py` | — | **new** — workload class logic tests |

---

# Phase 1 — Remove the `/stats/contributors` path

## Task 1: Remove `/stats/contributors` fetchers from `github_client.py`

**Files:**
- Modify: `src/github/github_client.py`

- [ ] **Step 1: Delete the three `/stats/contributors` symbols**

In `src/github/github_client.py`, delete entirely:
- `fetch_contributor_stats` (sync function, the `/repos/{repo}/stats/contributors` retry loop).
- `_fetch_stats_once` (async function — single `/stats/contributors` attempt).
- the `_NoStats` exception class.

**Keep** `_Deferred`, `_AsyncRateLimiter`, `_parse_next_link`, `_parse_last_page`,
`_fetch_total_commits`, `_fetch_total_contributors`, `_fetch_contributors_paginated`,
`_graphql`, `_sync_request`, `_session` — none touch `/stats/contributors`.

- [ ] **Step 2: Verify nothing else in `github_client.py` references the removed symbols**

Run: `grep -n "stats/contributors\|_fetch_stats_once\|_NoStats\|fetch_contributor_stats" src/github/github_client.py`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add src/github/github_client.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor(github): drop /stats/contributors fetchers (202-pathology)"
```

---

## Task 2: Remove the yearly/stats path from `fetch_contributors_metrics.py`

**Files:**
- Modify: `src/github/fetch_contributors_metrics.py`

- [ ] **Step 1: Delete the stats-only functions**

Delete entirely: `_week_in_range`, `_parse_api_stats`, `calculate_bus_factor`,
`_cumulative_loc`, `compute_yearly_breakdown`.

**Keep:** `parse_repo`, `_compute_bus_factor`, `compute_lifetime_metrics`, `main`.

- [ ] **Step 2: Fix the imports**

Replace the import block at the top of the file with:

```python
import argparse
import asyncio
import logging
import re

from src.github.models import (
    Contributor, PerfStats, RunResult,
    THRESHOLD, is_bot,
)
from src.github.batch_runner import batch_update
from src.pipeline.common.repos import VALUE_FILE, load_risk_slugs
```

(Removed: `datetime`, `time`, `DateRange`, `fetch_contributor_stats`, the
`src.github.display` import, `_upsert_yearly_csv`. None of the surviving
functions — `parse_repo`, `_compute_bus_factor`, `compute_lifetime_metrics`,
the new `main` — reference them. `re` stays: `parse_repo` uses it.)

- [ ] **Step 3: Rewrite `main()` to use the `/contributors` path for both modes**

Replace the entire body of `main()` with:

```python
def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate bus factor for a GitHub repo")
    parser.add_argument("repo", nargs="?", help="GitHub repo URL or owner/repo slug")
    parser.add_argument("--input", default=VALUE_FILE,
                        help=f"value-data CSV (default: {VALUE_FILE} — "
                             f"loads A/B class repos with non-empty github_repo, skips archived)")
    parser.add_argument("--limit", type=int, help="Process N random repos from --input CSV")
    parser.add_argument("--threshold", type=float, default=THRESHOLD,
                        help="Ownership threshold (default 0.5)")
    parser.add_argument("--include-bots", action="store_true", default=False,
                        help="Include bots as regular contributors in bus factor calculation")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch all repos, ignoring the concentration-data.csv freshness gate")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.limit and args.repo:
        parser.error("--limit requires batch mode (no repo argument)")

    if args.verbose:
        logging.getLogger("src.github").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Single-repo mode: fetch just that repo (always forced — an explicit
    # ad-hoc request should not be skipped by the freshness gate).
    if args.repo:
        repo = parse_repo(args.repo)
        asyncio.run(batch_update(
            [repo], threshold=args.threshold,
            include_bots=args.include_bots, force=True,
        ))
        return

    # Batch mode: every A/B-class repo from value-data.csv.
    repos = load_risk_slugs(value_file=args.input)
    asyncio.run(batch_update(
        repos, threshold=args.threshold,
        include_bots=args.include_bots, limit=args.limit, force=args.force,
    ))
```

(`batch_update`'s new signature — without `year_start`, `year_end`, `output`,
`base` — is defined in Task 4. `Contributor` / `PerfStats` / `RunResult` /
`THRESHOLD` / `is_bot` stay imported because `compute_lifetime_metrics` and
`_compute_bus_factor` still reference them.)

- [ ] **Step 4: Update the module docstring**

Replace the top docstring with:

```python
"""Contributor metrics — lifetime bus factor + HHI from GitHub's
/repos/{repo}/contributors endpoint.

Contributors are keyed by GitHub login. The /stats/contributors endpoint
(per-week, time-windowed) is intentionally NOT used — it returns HTTP 202
"computing" indefinitely for most repos, so every metric here is a
lifetime aggregate at fetch time.

Usage:
    python -m src.github.fetch_contributors_metrics facebook/react   # one repo
    python -m src.github.fetch_contributors_metrics                  # batch: value-data.csv A/B repos
"""
```

- [ ] **Step 5: Verify the module imports cleanly**

Run: `uv run python -c "import src.github.fetch_contributors_metrics"`
Expected: no error (no output).

Run: `grep -n "stats/contributors\|compute_yearly\|_parse_api_stats\|calculate_bus_factor" src/github/fetch_contributors_metrics.py`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add src/github/fetch_contributors_metrics.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor(github): single-repo CLI uses /contributors lifetime path"
```

---

## Task 3: Remove `display_yearly_breakdown` from `display.py`

**Files:**
- Modify: `src/github/display.py`

- [ ] **Step 1: Delete `display_yearly_breakdown`**

Delete the entire `display_yearly_breakdown` function.

- [ ] **Step 2: Confirm no remaining callers**

Run: `grep -rn "display_yearly_breakdown" src/ tests/`
Expected: no output.

- [ ] **Step 3: Verify**

Run: `uv run python -c "import src.github.display"`
Expected: no error.

- [ ] **Step 4: Commit**

```bash
git add src/github/display.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor(github): drop unused display_yearly_breakdown"
```

---

## Task 4: Remove wide-file machinery from `batch_runner.py`

**Files:**
- Modify: `src/github/batch_runner.py`

- [ ] **Step 1: Delete the wide-file functions and constants**

Delete entirely: `_build_metrics_extractors`, `_upsert_yearly_csv`,
`_upsert_yearly_csv_batch`, `_upsert_wide_file`, `_upsert_years_file`,
`_read_existing_periods`, `_is_single_year`, and the constants `WIDE_FILES`,
`YEARS_FILE`, `YEARS_FIELDS`.

**Keep:** `_load_repo_id_map`, `_recently_fetched_repos`, `_load_repos_from_csv`,
`_upsert_concentration_data`, `batch_update`, `CONCENTRATION_FILE`,
`CONCENTRATION_FIELDS`, `CONCENTRATION_TTL_DAYS`, `GH_REPOS_FILE`,
the imports, the `_RepoResult` dataclass.

- [ ] **Step 2: Fix `_upsert_concentration_data` — it can no longer depend on `_is_single_year`**

In `_upsert_concentration_data`, the aggregate-label picker currently does
`next((lbl for lbl in batch_labels if not _is_single_year(lbl)), None)`.
`compute_lifetime_metrics` always emits exactly one label (`"2021-2025"`), so
replace the picker with simply taking the first label:

```python
    agg_label = batch_labels[0] if batch_labels else None
    if agg_label is None:
        return
```

- [ ] **Step 3: Drop the `output`, `year_start`, `year_end`, `base` params from `batch_update`**

Change the `batch_update` signature to:

```python
async def batch_update(
    repos: list[str],
    threshold: float = THRESHOLD,
    include_bots: bool = False, limit: int | None = None,
    force: bool = False,
) -> None:
```

Inside `batch_update`:
- Replace `total_label = f"{year_start}-{year_end}"` with `total_label = "2021-2025"`
  (a fixed internal label for the single lifetime `RunResult`).
- Replace every `_upsert_yearly_csv_batch(output, pending_flush)` call with
  `_upsert_concentration_data(pending_flush, ["2021-2025"])`.
- The `import random` and `from src.github.fetch_contributors_metrics import
  compute_lifetime_metrics` lines inside the function stay.

- [ ] **Step 4: Update the CSV-I/O comment block**

Replace the `# --- CSV I/O ---` comment block (the one listing
`{dir}/bus-factor.csv` etc.) with:

```python
# --- CSV I/O ---
#
# Contributor metrics are persisted to a single file, data/concentration-data.csv,
# one row per repo: repo, repo_id, total_commits, total_contributors,
# active_contributors, bus_factor, hhi, fetched_at.
```

- [ ] **Step 5: Verify**

Run: `grep -n "stats/contributors\|WIDE_FILES\|_upsert_wide_file\|_upsert_yearly" src/github/batch_runner.py`
Expected: no output.

Run: `uv run python -c "import src.github.batch_runner"`
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add src/github/batch_runner.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor(github): batch fetcher writes only concentration-data.csv"
```

---

## Task 5: Update `test_contrib_metrics.py` and `test_contributors.py`

**Files:**
- Modify: `tests/test_contrib_metrics.py`
- Modify: `tests/test_contributors.py`

- [ ] **Step 1: Trim `test_contrib_metrics.py`**

Replace the import block with:

```python
"""Tests for contributor metrics — repo parsing + DateRange model."""

import datetime

import pytest

from src.github.models import DateRange
from src.github.fetch_contributors_metrics import parse_repo
```

Delete these test classes entirely: `TestCalculateBusFactor`, `TestHHI`,
`TestFetchContributorStats`, `TestFetchStatsOnce`.

Keep `TestParseRepo` unchanged.

Keep `TestDateRange`, but delete its `test_date_range_filter` method (it calls
the removed `calculate_bus_factor`).

- [ ] **Step 2: Trim `test_contributors.py`**

Replace the import block with:

```python
"""Tests for contributors module — _compute_bus_factor + bot filtering."""

import pytest

from src.github.models import Contributor, is_bot
from src.github.fetch_contributors_metrics import _compute_bus_factor
```

Delete these test classes entirely: `TestParseApiStats`, `TestComputeYearlyBreakdown`.

Keep `TestComputeBusFactor` and `TestIsBotDetection` unchanged.

- [ ] **Step 3: Run the trimmed test files**

Run: `uv run pytest tests/test_contrib_metrics.py tests/test_contributors.py -v`
Expected: PASS — all remaining tests green, no import errors.

- [ ] **Step 4: Commit**

```bash
git add tests/test_contrib_metrics.py tests/test_contributors.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "test(github): drop tests for removed /stats/contributors functions"
```

---

# Phase 2 — Consolidate contributor data

## Task 6: Add `active_contributors` to the concentration-data writer

**Files:**
- Modify: `src/github/batch_runner.py`

- [ ] **Step 1: Add `active_contributors` to `CONCENTRATION_FIELDS`**

```python
CONCENTRATION_FIELDS = [
    "repo", "repo_id",
    "total_commits", "total_contributors", "active_contributors",
    "bus_factor", "hhi", "fetched_at",
]
```

- [ ] **Step 2: Write `active_contributors` in `_upsert_concentration_data`**

In `_upsert_concentration_data`, the per-repo row builder already computes
`humans = [c for c in agg.contributors if not c.is_bot]`. Add one key to the
`existing[repo] = {...}` dict, right after `total_contributors`:

```python
            "active_contributors": str(len(humans)),
```

- [ ] **Step 3: Verify**

Run: `uv run python -c "import src.github.batch_runner"`
Expected: no error.

- [ ] **Step 4: Commit**

```bash
git add src/github/batch_runner.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat(github): record active_contributors (non-bot count) in concentration-data"
```

---

## Task 7: One-time migration — backfill `active_contributors`

**Files:**
- Modify: `data/concentration-data.csv`

- [ ] **Step 1: Run the migration**

The non-bot contributor count is already stored in
`data/sources/github/contributors/contributors.csv` under the `2021-2025` column.
Carry it into `concentration-data.csv` so no multi-hour re-fetch is needed.
Run this exact command:

```bash
uv run python - <<'EOF'
import csv

contrib = {}
with open("data/sources/github/contributors/contributors.csv", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        slug = (row.get("repo") or "").strip().lower()
        if slug:
            contrib[slug] = (row.get("2021-2025") or "").strip()

FIELDS = ["repo", "repo_id", "total_commits", "total_contributors",
          "active_contributors", "bus_factor", "hhi", "fetched_at"]

rows = []
with open("data/concentration-data.csv", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        slug = (row.get("repo") or "").strip().lower()
        row["active_contributors"] = contrib.get(slug, "")
        rows.append(row)

with open("data/concentration-data.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)

filled = sum(1 for r in rows if r["active_contributors"])
print(f"{len(rows)} rows, {filled} with active_contributors ({100*filled/len(rows):.1f}%)")
EOF
```

Expected: prints `~929 rows, ~925 with active_contributors (~99.6%)`.

- [ ] **Step 2: Verify the new header**

Run: `head -1 data/concentration-data.csv`
Expected: `repo,repo_id,total_commits,total_contributors,active_contributors,bus_factor,hhi,fetched_at`

- [ ] **Step 3: Commit**

```bash
git add data/concentration-data.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "data: backfill active_contributors into concentration-data.csv"
```

---

## Task 8: Delete the stale wide CSVs

**Files:**
- Delete: `data/sources/github/contributors/{bus-factor,hhi,contributors,bots,commits}.csv`, `years.csv`

- [ ] **Step 1: Remove the files**

```bash
git rm data/sources/github/contributors/bus-factor.csv \
       data/sources/github/contributors/hhi.csv \
       data/sources/github/contributors/contributors.csv \
       data/sources/github/contributors/bots.csv \
       data/sources/github/contributors/commits.csv \
       data/sources/github/contributors/years.csv
```

(If `git rm` reports a file is not tracked, delete it with plain `rm` instead.)

- [ ] **Step 2: Verify the directory is empty, then remove it**

Run: `ls -A data/sources/github/contributors 2>/dev/null`
Expected: empty output. Then: `rmdir data/sources/github/contributors 2>/dev/null || true`

- [ ] **Step 3: Commit**

```bash
git add -A data/sources/github/contributors
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "data: delete stale per-year contributor CSVs (consolidated to concentration-data.csv)"
```

---

## Task 9: Point `build_concentration.py` at `concentration-data.csv` only

**Files:**
- Modify: `src/pipeline/risk/build_concentration.py`

- [ ] **Step 1: Remove the wide-file constants and loader**

Delete: `CONTRIB_DIR`, `HHI_FILE`, `BF_FILE`, `CONTRIB_FILE`, `COMMITS_FILE`,
`AGG_COL`, and the `_load_agg_column` function. Keep `LIFETIME_FILE`,
`OUTPUT_FILE`, `_load_lifetime`.

- [ ] **Step 2: Add `active_contributors` to `FIELDS`**

```python
FIELDS = [
    "repo", "repo_id",
    "total_commits_lifetime", "total_contributors_lifetime",
    "active_contributors",
    "hhi_commits_lifetime", "bf_commits_lifetime",
    "fetched_at",
]
```

- [ ] **Step 3: Rewrite `build()` to read every value from `concentration-data.csv`**

```python
def build() -> list[dict]:
    eligible = load_risk_repos()
    lifetime = _load_lifetime()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        lt = lifetime.get(repo, {})
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "total_commits_lifetime": (lt.get("total_commits") or "").strip(),
            "total_contributors_lifetime": (lt.get("total_contributors") or "").strip(),
            "active_contributors": (lt.get("active_contributors") or "").strip(),
            "hhi_commits_lifetime": (lt.get("hhi") or "").strip(),
            "bf_commits_lifetime": (lt.get("bus_factor") or "").strip(),
            "fetched_at": (lt.get("fetched_at") or "").strip(),
        })
    return rows
```

- [ ] **Step 4: Add `active_contributors` to the coverage table in `main()`**

In `main()`, add `"active_contributors"` to the column tuple passed to the
coverage-table loop (after `total_contributors_lifetime`).

- [ ] **Step 5: Update the module docstring**

Update the `Reads:` and `Writes:` sections of the docstring: the only input is
`data/concentration-data.csv` (the wide per-year files no longer exist), and
the output gains an `active_contributors` column (lifetime distinct non-bot
contributors — a floor for repos with >5000 contributors).

- [ ] **Step 6: Run the builder**

Run: `uv run python -m src.pipeline.risk.build_concentration`
Expected: prints a coverage table; `active_contributors` row shows ~99% coverage;
writes `data/risk/concentration.csv`.

Run: `head -1 data/risk/concentration.csv`
Expected: header contains `active_contributors`.

- [ ] **Step 7: Commit**

```bash
git add src/pipeline/risk/build_concentration.py data/risk/concentration.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat(risk): build_concentration emits active_contributors from concentration-data.csv"
```

---

## Task 10: Replace the wide-file tests in `test_batch.py`

**Files:**
- Modify: `tests/test_batch.py`

- [ ] **Step 1: Replace the whole file with the trimmed version**

Overwrite `tests/test_batch.py` with:

```python
"""Tests for batch module — concentration-data I/O and repo loading (no API calls)."""

import csv

import pytest

from src.github.models import Contributor, RunResult, PerfStats
from src.github.batch_runner import (
    _upsert_concentration_data,
    _load_repos_from_csv,
    CONCENTRATION_FIELDS,
)


def _make_result(contributors, bus_factor=1, hhi=0.5, total_commits=None,
                 total_contributors=None):
    return RunResult(
        bus_factor=bus_factor,
        contributors=contributors,
        hhi=hhi,
        perf=PerfStats(),
        total_commits=total_commits,
        total_contributors=total_contributors,
    )


def _make_contribs(specs):
    """specs: list of (login, commits, lines_changed, is_bot)."""
    contribs = []
    for login, commits, lines, is_bot in specs:
        c = Contributor(login=login, commits=commits, lines_changed=lines)
        c.is_bot = is_bot
        contribs.append(c)
    return contribs


class TestConcentrationFields:
    def test_active_contributors_present(self):
        assert "active_contributors" in CONCENTRATION_FIELDS


class TestUpsertConcentrationData:
    def test_writes_active_contributors_count(self, tmp_path, monkeypatch):
        # 2 humans + 1 bot → active_contributors == "2".
        contribs = _make_contribs([
            ("alice", 100, 500, False),
            ("bob", 50, 200, False),
            ("dependabot[bot]", 30, 100, True),
        ])
        result = _make_result(contribs, bus_factor=1, hhi=0.5,
                              total_commits=180, total_contributors=3)

        conc_file = tmp_path / "concentration-data.csv"
        monkeypatch.setattr("src.github.batch_runner.CONCENTRATION_FILE", str(conc_file))
        monkeypatch.setattr("src.github.batch_runner._load_repo_id_map", lambda: {})

        _upsert_concentration_data([("owner/repo", [("2021-2025", result)])], ["2021-2025"])

        with open(conc_file, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["repo"] == "owner/repo"
        assert rows[0]["active_contributors"] == "2"
        assert rows[0]["bus_factor"] == "1"

    def test_preserves_other_repos(self, tmp_path, monkeypatch):
        conc_file = tmp_path / "concentration-data.csv"
        conc_file.write_text(
            "repo,repo_id,total_commits,total_contributors,active_contributors,"
            "bus_factor,hhi,fetched_at\n"
            "keep/me,1,10,2,2,1,5000,2026-01-01T00:00:00+00:00\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("src.github.batch_runner.CONCENTRATION_FILE", str(conc_file))
        monkeypatch.setattr("src.github.batch_runner._load_repo_id_map", lambda: {})

        contribs = _make_contribs([("alice", 100, 500, False)])
        result = _make_result(contribs, bus_factor=1, hhi=1.0,
                              total_commits=100, total_contributors=1)
        _upsert_concentration_data([("new/repo", [("2021-2025", result)])], ["2021-2025"])

        with open(conc_file, encoding="utf-8") as f:
            rows = {r["repo"]: r for r in csv.DictReader(f)}
        assert set(rows) == {"keep/me", "new/repo"}
        assert rows["keep/me"]["active_contributors"] == "2"


class TestLoadReposFromCsv:
    def test_loads_repos(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo,stars\nowner/a,10\nowner/b,5\n", encoding="utf-8")
        assert _load_repos_from_csv(str(f)) == ["owner/a", "owner/b"]

    def test_skips_archived(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo,archived\nowner/a,false\nowner/b,true\n", encoding="utf-8")
        assert _load_repos_from_csv(str(f)) == ["owner/a"]

    def test_lowercases_repos(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo\nOwner/Repo\n", encoding="utf-8")
        assert _load_repos_from_csv(str(f)) == ["owner/repo"]

    def test_no_stars_column(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo\nowner/a\nowner/b\n", encoding="utf-8")
        assert sorted(_load_repos_from_csv(str(f))) == ["owner/a", "owner/b"]
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_batch.py -v`
Expected: PASS — all tests green.

- [ ] **Step 3: Commit**

```bash
git add tests/test_batch.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "test(github): test concentration-data I/O incl. active_contributors"
```

---

# Phase 3 — `workload_class`

## Task 11: Pure workload-class helpers + tests (TDD)

**Files:**
- Create: `tests/test_build_workload.py`
- Modify: `src/pipeline/risk/build_workload.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_build_workload.py`:

```python
"""Tests for src/pipeline/risk/build_workload.py — workload class logic."""

import pytest

from src.pipeline.risk.build_workload import (
    _hazen_percentiles,
    _geometric_mean,
    _quartile_classes,
    compute_workload_classes,
)


class TestHazenPercentiles:
    def test_strictly_between_0_and_100(self):
        # Even the minimum value must be > 0 (so a geometric mean can't collapse).
        pctls = _hazen_percentiles([5, 1, 9, 3])
        assert all(0 < p < 100 for p in pctls)

    def test_monotonic_with_value(self):
        # Higher input value → higher percentile.
        pctls = _hazen_percentiles([10, 20, 30, 40])
        assert pctls == sorted(pctls)
        assert pctls[0] < pctls[-1]

    def test_negative_values_rank_low(self):
        # A negative ratio (e.g. negative NNI) must rank at the low end.
        vals = [-5.0, 0.0, 10.0, 50.0]
        pctls = _hazen_percentiles(vals)
        assert pctls[0] == min(pctls)

    def test_ties_share_average_rank(self):
        pctls = _hazen_percentiles([7, 7, 7, 7])
        assert pctls[0] == pctls[1] == pctls[2] == pctls[3]
        assert pctls[0] == pytest.approx(50.0)  # 100*(2.5-0.5)/4

    def test_empty(self):
        assert _hazen_percentiles([]) == []


class TestGeometricMean:
    def test_known_value(self):
        assert _geometric_mean([4.0, 9.0]) == pytest.approx(6.0)

    def test_three_values(self):
        assert _geometric_mean([8.0, 8.0, 8.0]) == pytest.approx(8.0)

    def test_empty(self):
        assert _geometric_mean([]) == 0.0


class TestQuartileClasses:
    def test_equal_count_split(self):
        # 8 distinct scores → 2 per class, A = highest.
        classes = _quartile_classes([1, 2, 3, 4, 5, 6, 7, 8])
        assert classes == ["D", "D", "C", "C", "B", "B", "A", "A"]

    def test_remainder_within_one(self):
        # 10 scores → each class holds 2 or 3.
        from collections import Counter
        classes = _quartile_classes(list(range(10)))
        counts = Counter(classes)
        assert all(c in (2, 3) for c in counts.values())
        assert set(counts) == {"A", "B", "C", "D"}

    def test_highest_score_is_class_a(self):
        classes = _quartile_classes([10, 99, 50, 1])
        assert classes[1] == "A"  # 99 is the highest

    def test_empty(self):
        assert _quartile_classes([]) == []


class TestComputeWorkloadClasses:
    @staticmethod
    def _metric(repo, loc, cve, nni, ac):
        return {"repo": repo, "loc": loc, "cve": cve, "nni": nni, "ac": ac}

    def test_classifies_repos_with_all_inputs(self):
        metrics = [
            self._metric(f"r/{i}", loc=i * 1000.0, cve=float(i), nni=float(i), ac=2.0)
            for i in range(1, 9)
        ]
        out = compute_workload_classes(metrics)
        assert set(out) == {f"r/{i}" for i in range(1, 9)}
        # r/8 carries the most burden per contributor → class A.
        assert out["r/8"]["workload_class"] == "A"
        assert out["r/1"]["workload_class"] == "D"

    def test_missing_cve_yields_empty_class(self):
        metrics = [
            self._metric("r/a", loc=1000.0, cve=None, nni=5.0, ac=2.0),
            self._metric("r/b", loc=2000.0, cve=3.0, nni=5.0, ac=2.0),
            self._metric("r/c", loc=3000.0, cve=4.0, nni=5.0, ac=2.0),
        ]
        out = compute_workload_classes(metrics)
        assert out["r/a"]["workload_class"] == ""
        assert out["r/a"]["loc_per_ac"] == ""

    def test_zero_ac_yields_empty_class(self):
        metrics = [
            self._metric("r/a", loc=1000.0, cve=3.0, nni=5.0, ac=0.0),
            self._metric("r/b", loc=2000.0, cve=3.0, nni=5.0, ac=2.0),
        ]
        out = compute_workload_classes(metrics)
        assert out["r/a"]["workload_class"] == ""

    def test_negative_nni_still_classified(self):
        # A repo closing issues faster than it opens them (negative NNI)
        # must still receive a class — it just lands in the low-burden tail.
        metrics = [
            self._metric("r/neg", loc=500.0, cve=0.0, nni=-40.0, ac=4.0),
        ] + [
            self._metric(f"r/{i}", loc=i * 1000.0, cve=float(i), nni=float(i * 10), ac=4.0)
            for i in range(1, 8)
        ]
        out = compute_workload_classes(metrics)
        assert out["r/neg"]["workload_class"] != ""
        assert out["r/neg"]["nni_per_ac"] == pytest.approx(-10.0)

    def test_ratios_rounded(self):
        metrics = [
            self._metric(f"r/{i}", loc=1000.0, cve=2.0, nni=4.0, ac=3.0)
            for i in range(8)
        ]
        out = compute_workload_classes(metrics)
        assert out["r/0"]["loc_per_ac"] == pytest.approx(333.3333, abs=1e-3)
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_build_workload.py -v`
Expected: FAIL with `ImportError: cannot import name '_hazen_percentiles'`.

- [ ] **Step 3: Implement the helpers in `build_workload.py`**

Add these four functions to `src/pipeline/risk/build_workload.py` (after the
imports, before `_load_repo_meta`):

```python
def _hazen_percentiles(values: list[float]) -> list[float]:
    """Percentile-rank each value via the Hazen plotting position.

    pct = 100 * (rank - 0.5) / n, with tied values sharing the average of
    their ranks. The result is strictly within (0, 100) — never exactly 0
    or 100 — so a geometric mean taken over these percentiles cannot
    collapse to 0. Higher value → higher percentile. Empty input → [].
    """
    n = len(values)
    if n == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda iv: iv[1])
    pctls = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based, tie-averaged
        pct = 100.0 * (avg_rank - 0.5) / n
        for k in range(i, j + 1):
            pctls[indexed[k][0]] = pct
        i = j + 1
    return pctls


def _geometric_mean(values: list[float]) -> float:
    """Geometric mean (∏ v)^(1/n). Assumes every value > 0; [] → 0.0."""
    if not values:
        return 0.0
    product = 1.0
    for v in values:
        product *= v
    return product ** (1.0 / len(values))


def _quartile_classes(scores: list[float]) -> list[str]:
    """Assign A/B/C/D by equal-count quartiles of `scores` (higher = worse).

    Sorted descending, the highest-scoring 25% get 'A', then 'B', 'C', 'D'.
    When n is not divisible by 4 each class holds ⌊n/4⌋ or ⌈n/4⌉ members.
    Empty input → [].
    """
    n = len(scores)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)
    labels = ["A", "B", "C", "D"]
    out = [""] * n
    for p, idx in enumerate(order):  # p: 0-based rank, 0 = highest score
        out[idx] = labels[min(3, p * 4 // n)]
    return out


def compute_workload_classes(metrics: list[dict]) -> dict[str, dict]:
    """Compute per-maintainer burden ratios, percentiles, and the class.

    `metrics` — one dict per repo with keys `repo`, `loc`, `cve`, `nni`,
    `ac`. `loc`/`cve`/`ac` are floats or None (None = the underlying
    metric is missing); `nni` is always a float and may be negative.

    Returns {repo: {...}} with these keys per repo:
        loc_per_ac, cve_per_ac, nni_per_ac,
        loc_per_ac_pctl, cve_per_ac_pctl, nni_per_ac_pctl,
        workload_burden_percentile, workload_class
    A repo is classified only when loc, cve, nni, and ac are all present
    AND ac > 0; otherwise every value is the empty string "".
    """
    keys = ("loc_per_ac", "cve_per_ac", "nni_per_ac",
            "loc_per_ac_pctl", "cve_per_ac_pctl", "nni_per_ac_pctl",
            "workload_burden_percentile", "workload_class")
    out: dict[str, dict] = {m["repo"]: {k: "" for k in keys} for m in metrics}

    # 1. Keep only repos with all four inputs present and ac > 0.
    classifiable: list[dict] = []
    for m in metrics:
        loc, cve, nni, ac = m["loc"], m["cve"], m["nni"], m["ac"]
        if loc is None or cve is None or nni is None or ac is None or ac <= 0:
            continue
        classifiable.append({
            "repo": m["repo"],
            "loc_per_ac": loc / ac,
            "cve_per_ac": cve / ac,
            "nni_per_ac": nni / ac,
        })
    if not classifiable:
        return out

    # 2. Hazen-percentile each ratio across the classifiable set.
    loc_p = _hazen_percentiles([c["loc_per_ac"] for c in classifiable])
    cve_p = _hazen_percentiles([c["cve_per_ac"] for c in classifiable])
    nni_p = _hazen_percentiles([c["nni_per_ac"] for c in classifiable])

    # 3. Geometric mean of the three percentiles → burden score.
    burden = [_geometric_mean([loc_p[i], cve_p[i], nni_p[i]])
              for i in range(len(classifiable))]

    # 4. Equal-count quartile class (A = highest burden).
    classes = _quartile_classes(burden)

    # 5. Emit.
    for i, c in enumerate(classifiable):
        out[c["repo"]] = {
            "loc_per_ac": round(c["loc_per_ac"], 4),
            "cve_per_ac": round(c["cve_per_ac"], 4),
            "nni_per_ac": round(c["nni_per_ac"], 4),
            "loc_per_ac_pctl": round(loc_p[i], 2),
            "cve_per_ac_pctl": round(cve_p[i], 2),
            "nni_per_ac_pctl": round(nni_p[i], 2),
            "workload_burden_percentile": round(burden[i], 2),
            "workload_class": classes[i],
        }
    return out
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_build_workload.py -v`
Expected: PASS — all tests green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_build_workload.py src/pipeline/risk/build_workload.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat(risk): workload class helpers — Hazen percentile, geom mean, quartiles"
```

---

## Task 12: Wire `workload_class` into `build_workload.py`'s `build()`

**Files:**
- Modify: `src/pipeline/risk/build_workload.py`

- [ ] **Step 1: Add the new input-file constants**

After the existing `OUTPUT_FILE = DATA_DIR / "risk" / "workload.csv"` line, add:

```python
COMPLEXITY_FILE = DATA_DIR / "risk" / "complexity.csv"
SECURITY_FILE = DATA_DIR / "risk" / "security.csv"
CONCENTRATION_FILE = DATA_DIR / "risk" / "concentration.csv"
```

Remove the now-obsolete `CONTRIB_FILE` constant (it pointed at the deleted
`data/sources/github/contributors/contributors.csv`). Also delete the
`_load_wide_year` function — after the `build()` rewrite in Step 4 nothing
calls it (it only ever loaded the deleted `contributors.csv`).

- [ ] **Step 2: Update `FIELDS`**

Replace the `FIELDS` list with:

```python
FIELDS = [
    "repo", "repo_id",
    "repo_age_years_2025_eoy",
    "active_contributors",
    "openssf_maintained",
    "has_issues",
    "push_cadence_years", "pushed_at",
    "issues_opened_5y", "issues_closed_5y", "issue_close_ratio",
    "net_new_issues_5y",
    "slope_opened", "slope_closed", "issue_trend_score",
    "loc_per_ac", "cve_per_ac", "nni_per_ac",
    "loc_per_ac_pctl", "cve_per_ac_pctl", "nni_per_ac_pctl",
    "workload_burden_percentile", "workload_class",
    "fetched_at",
]
```

- [ ] **Step 3: Add a generic single-column loader and a number parser**

Add these two helpers near the other `_load_*` functions:

```python
def _load_column(path: Path, column: str) -> dict[str, str]:
    """Return {repo_lowercased: value} for one column of a wide CSV."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out[slug] = (row.get(column) or "").strip()
    return out


def _num(value: str) -> float | None:
    """Parse a CSV cell to float. Empty / unparseable → None."""
    s = (value or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
```

- [ ] **Step 4: Rewrite `build()` to load the cross-dimension inputs and attach the class**

Replace the `build()` function with:

```python
def build() -> list[dict]:
    eligible = load_risk_repos()

    repos = _load_repo_meta()
    commits_years = _load_commits_years()
    maintained = _load_openssf_maintained()
    issues = _load_issues_long(ISSUES_FILE)
    opened = issues["opened_issues"]
    closed = issues["closed_issues"]

    # Cross-dimension inputs for the workload class.
    loc_by_repo = _load_column(COMPLEXITY_FILE, "loc_2025_eoy")
    cve_by_repo = _load_column(SECURITY_FILE, "cve_count_5y")
    ac_by_repo = _load_column(CONCENTRATION_FILE, "active_contributors")

    rows: list[dict] = []
    metrics: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        meta = repos.get(repo, {})

        # Age
        created_at = (meta.get("created_at") or "").strip()
        age = _repo_age_years(created_at)

        # has_issues
        hi_raw = (meta.get("has_issues") or "").strip()
        has_issues = hi_raw if hi_raw else ""

        # Push cadence: years with ≥1 commit in 2021-2025
        cy = commits_years.get(repo, {})
        cadence = sum(1 for y in YEARS if cy.get(y, 0) > 0) if cy else ""
        cadence_val = str(cadence) if cadence != "" else ""

        # OpenSSF maintained
        openssf_maintained = maintained.get(repo, "")

        # Issues
        op = opened.get(repo, {})
        cl = closed.get(repo, {})
        op_5y = sum(op.values())
        cl_5y = sum(cl.values())
        ratio = round(cl_5y / op_5y, 3) if op_5y > 0 else ""
        net_new_issues = op_5y - cl_5y

        op_vals = [op.get(y, 0) for y in YEARS]
        cl_vals = [cl.get(y, 0) for y in YEARS]
        s_open = _ols_slope(YEARS, op_vals)
        s_close = _ols_slope(YEARS, cl_vals)
        mean_op = op_5y / len(YEARS) if op_5y else 0
        if mean_op >= 1:
            trend_score = round((s_close - s_open) / mean_op, 4)
        else:
            trend_score = ""

        ac_raw = ac_by_repo.get(repo, "")

        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "repo_age_years_2025_eoy": age,
            "active_contributors": ac_raw,
            "openssf_maintained": openssf_maintained,
            "has_issues": has_issues,
            "push_cadence_years": cadence_val,
            "pushed_at": (meta.get("pushed_at") or "").strip(),
            "issues_opened_5y": op_5y,
            "issues_closed_5y": cl_5y,
            "issue_close_ratio": ratio,
            "net_new_issues_5y": net_new_issues,
            "slope_opened": (round(s_open, 2) if op_5y >= 1 else ""),
            "slope_closed": (round(s_close, 2) if op_5y >= 1 else ""),
            "issue_trend_score": trend_score,
            # workload-class columns — filled by the second pass below.
            "loc_per_ac": "", "cve_per_ac": "", "nni_per_ac": "",
            "loc_per_ac_pctl": "", "cve_per_ac_pctl": "", "nni_per_ac_pctl": "",
            "workload_burden_percentile": "", "workload_class": "",
            "fetched_at": (meta.get("fetched_at") or "").strip(),
        })
        metrics.append({
            "repo": repo,
            "loc": _num(loc_by_repo.get(repo, "")),
            "cve": _num(cve_by_repo.get(repo, "")),
            "nni": float(net_new_issues),
            "ac": _num(ac_raw),
        })

    # Second pass: percentile-rank + classify, then merge back by repo.
    workload = compute_workload_classes(metrics)
    for row in rows:
        row.update(workload.get(row["repo"], {}))
    return rows
```

- [ ] **Step 5: Update the coverage table in `main()`**

In `main()`, replace the column tuple in the coverage-table loop with:

```python
    for col in (
        "repo_age_years_2025_eoy", "active_contributors",
        "openssf_maintained", "has_issues", "push_cadence_years", "pushed_at",
        "issue_close_ratio", "net_new_issues_5y", "issue_trend_score",
        "workload_burden_percentile", "workload_class",
    ):
```

Then, after `console.print(table)` and before the final `console.print(f"...Wrote...")`,
add a workload-class distribution table:

```python
    from collections import Counter
    cls = Counter(r["workload_class"] or "—" for r in rows)
    ctable = Table(title="\n[bold]Workload class[/bold]",
                   show_header=True, header_style="bold dim", padding=(0, 1))
    ctable.add_column("Class", style="bold")
    ctable.add_column("Repos", justify="right")
    for label in ("A", "B", "C", "D", "—"):
        ctable.add_row(label, f"{cls.get(label, 0):,}")
    console.print(ctable)
```

- [ ] **Step 6: Add the import for `compute_workload_classes`**

`compute_workload_classes`, `_hazen_percentiles`, etc. are defined in the same
module (Task 11) — no import needed. Confirm `csv` and `Path` are already
imported at the top (they are).

- [ ] **Step 7: Update the module docstring**

Update the `Reads:` block to add `data/risk/complexity.csv`, `data/risk/security.csv`,
`data/risk/concentration.csv`; update the `Writes:` block to list the new columns
(`active_contributors`, `net_new_issues_5y`, the three `*_per_ac`, the three
`*_per_ac_pctl`, `workload_burden_percentile`, `workload_class`); note that
`workload_class` is empty unless LOC, CVE, NNI, and AC are all present with
AC > 0, and that `build_workload` must run after `build_complexity`,
`build_security`, and `build_concentration`.

- [ ] **Step 8: Run the builder against real data**

(`build_concentration` ran in Task 9. Ensure `complexity.csv` and
`security.csv` exist — if not, run `uv run python -m src.pipeline.risk.build_complexity`
and `uv run python -m src.pipeline.risk.build_security` first.)

Run: `uv run python -m src.pipeline.risk.build_workload`
Expected: prints the coverage table + a "Workload class" table where A/B/C/D
each hold roughly a quarter of the classified repos and `—` holds the
unclassified remainder; writes `data/risk/workload.csv`.

Run: `head -1 data/risk/workload.csv`
Expected: header ends with `...workload_burden_percentile,workload_class,fetched_at`.

- [ ] **Step 9: Run the workload tests again (regression)**

Run: `uv run pytest tests/test_build_workload.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/pipeline/risk/build_workload.py data/risk/workload.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat(risk): add workload_class to build_workload (LOC/CVE/NNI per contributor)"
```

---

## Task 13: Document the class in `settings.json`

**Files:**
- Modify: `src/pipeline/settings.json`

- [ ] **Step 1: Add the `workload` block under `risk_classification`**

In `src/pipeline/settings.json`, inside the `risk_classification` object, add a
`workload` entry after the `funding` block (mind the trailing comma on
`funding`):

```json
    "workload": {
      "method": "geom_mean_quartile",
      "ratios": ["loc_per_ac", "cve_per_ac", "nni_per_ac"],
      "comment": "workload_class = equal-count quartiles (A = worst 25%) of the geometric mean of Hazen percentiles of LOC/AC, CVE/AC, NNI/AC. AC = active_contributors (lifetime non-bot contributors). NNI = issues_opened_5y - issues_closed_5y. Parameter-free; no numeric thresholds."
    }
```

- [ ] **Step 2: Verify the JSON is valid**

Run: `uv run python -c "import json; json.load(open('src/pipeline/settings.json'))"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add src/pipeline/settings.json
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "docs(settings): document workload_class method"
```

---

## Task 14: Update `docs/risk.md`

**Files:**
- Modify: `docs/risk.md`

- [ ] **Step 1: Add the Workload Class table**

After the existing "### Issue Trend" subsection (and before "## Data Sources"),
add:

```markdown
### Workload Class

Per-contributor burden, combining codebase size, security debt, and issue
backlog. For each repo three ratios are formed (▴ higher = more workload):

- `loc_per_ac` — lines of code per active contributor
- `cve_per_ac` — CVEs (5y) per active contributor
- `nni_per_ac` — net new issues (opened − closed, 5y) per active contributor

`AC` = `active_contributors`, the lifetime distinct non-bot contributor count.
Each ratio is percentile-ranked across the eligible set (Hazen position
`100·(rank−0.5)/n`, strictly in 0–100); `workload_burden_percentile` is the
geometric mean of the three percentiles. The class is its equal-count quartile:

| Class | Label | Criteria |
|-------|-------|----------|
| **A** | overloaded | top 25% of `workload_burden_percentile` |
| **B** | high | next 25% |
| **C** | moderate | next 25% |
| **D** | comfortable | bottom 25% |
| _empty_ | no signal | LOC, CVE, NNI, or AC missing, or AC = 0 |
```

- [ ] **Step 2: Update the metrics roadmap**

In the `Risk` tree near the top of the file, in the "Maintainer workload"
branch, change the `active_maintainers` leaf to:

```
    ├── active_contributors    ← GitHub /contributors (non-bot count)    [lifetime]
```

- [ ] **Step 3: Update the `risk-data.csv` output-column table**

In the "### risk-data.csv" output table, add rows for the new workload columns:
`active_contributors`, `net_new_issues_5y`, `loc_per_ac`, `cve_per_ac`,
`nni_per_ac`, `loc_per_ac_pctl`, `cve_per_ac_pctl`, `nni_per_ac_pctl`,
`workload_burden_percentile`, `workload_class` — each with a one-line
description matching the definitions above.

- [ ] **Step 4: Update the "Source-file coverage" section**

In the source-file coverage table, remove the
`data/sources/github/contributors/contributors.csv` row (and any `bus-factor` /
`hhi` / `commits` wide-file rows). Note in the surrounding prose that
contributor metrics now live solely in `data/concentration-data.csv`, and that
the `/stats/contributors` per-year breakdown has been retired.

- [ ] **Step 5: Commit**

```bash
git add docs/risk.md
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "docs(risk): document workload class + contributor-source consolidation"
```

---

## Task 15: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `uv run pytest -q`
Expected: PASS — no failures, no import errors. (If unrelated pre-existing
failures appear, confirm they also fail on `main` before the branch; do not
fix unrelated tests here.)

- [ ] **Step 2: Confirm `/stats/contributors` is fully gone from `src/`**

Run: `grep -rn "stats/contributors" src/`
Expected: no output.

- [ ] **Step 3: Re-run the risk builders end-to-end and aggregate**

Run:
```bash
uv run python -m src.pipeline.risk.build_concentration
uv run python -m src.pipeline.risk.build_workload
uv run python -m src.pipeline.risk.aggregate_risk
```
Expected: each completes; `aggregate_risk` prints a coverage table that now
includes `active_contributors`, `workload_burden_percentile`, and
`workload_class`; `data/risk/risk.csv` is written.

- [ ] **Step 4: Spot-check the joined output**

Run:
```bash
uv run python - <<'EOF'
import csv
from collections import Counter
with open("data/risk/risk.csv", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
cls = Counter(r.get("workload_class") or "—" for r in rows)
print("workload_class:", dict(cls))
ac = sum(1 for r in rows if (r.get("active_contributors") or "").strip())
print(f"active_contributors populated: {ac}/{len(rows)}")
EOF
```
Expected: A/B/C/D each ≈ a quarter of the classified repos; `—` is the
unclassified remainder; `active_contributors` populated for ~99% of rows.

- [ ] **Step 5: Commit any regenerated data**

```bash
git add data/risk/risk.csv data/risk/concentration.csv data/risk/workload.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "data: regenerate risk intermediates with workload_class" || echo "nothing to commit"
```

---

## Done

`workload_class` is live in `risk-data.csv`; the `/stats/contributors` path and
its stale per-year CSVs are gone; `active_contributors` is the single
consolidated contributor count. Do **not** push — leave the branch for the user
to review (per `model/CLAUDE.md`).
