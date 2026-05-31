# Value → Risk Pipeline Rewire — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the risk pipeline consume A/B value-class repos straight from `value-data.csv` (target classes configured in the renamed `settings.json`) instead of `eligibility-data.csv`, include all classes in `value-data.csv`, update docs, then run the full risk pipeline and fix anomalies.

**Architecture:** One shared loader `load_risk_repos()` in `src/pipeline/repos.py` reads `value-data.csv`, filters `class ∈ settings.risk_input.value_classes`, enriches `repo_id`/`archived` from `data/sources/github/repos.csv`. Every risk script calls it. Config file `params.json` → `settings.json` (module `params.py` unchanged).

**Tech Stack:** Python 3, `uv`, `rich`, `pytest`. Spec: `docs/superpowers/specs/2026-05-17-value-to-risk-pipeline-rewire-design.md`.

**Facts:** `value-data.csv` = 2564 rows (A=226, B=728, C=1610, D dropped). A/B with `github_repo` = 929 (2 invalid). `data/sources/github/repos.csv` = 2465 rows, 2451 with `repo_id`, 90 archived. `.env` has `GITHUB_TOKENS`.

---

## Phase 1 — Foundation (sequential)

### Task 1: Rename config → `settings.json`

**Files:**
- Rename: `src/pipeline/params.json` → `src/pipeline/settings.json`
- Modify: `src/pipeline/params.py`

- [ ] **Step 1:** `git mv src/pipeline/params.json src/pipeline/settings.json`
- [ ] **Step 2:** Add a new top-level block to `settings.json` (after `value_classes`):

```json
  "risk_input": {
    "value_classes": ["A", "B"],
    "comment": "Risk pipeline runs on repos whose value-data.csv class is in this list."
  },
```

- [ ] **Step 3:** In `params.py` change `_PARAMS_PATH` filename `params.json` → `settings.json`. Add after the risk-classification exports:

```python
# Risk-pipeline input scope — which value classes feed the risk pipeline.
RISK_INPUT_CLASSES: list[str] = _P["risk_input"]["value_classes"]
```

- [ ] **Step 4:** Run `uv run python -c "from src.pipeline.params import RISK_INPUT_CLASSES; print(RISK_INPUT_CLASSES)"` — Expected: `['A', 'B']`
- [ ] **Step 5:** Commit: `chore: rename params.json → settings.json, add risk_input block`

### Task 2: Shared `load_risk_repos()` loader

**Files:**
- Modify: `src/pipeline/repos.py`
- Test: `tests/test_repos_loader.py`

- [ ] **Step 1: Write failing test** `tests/test_repos_loader.py`:

```python
import csv
from src.pipeline import repos


def _write(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def test_load_risk_repos_filters_classes_and_enriches(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "gh_valid", "class"], [
        {"github_repo": "Owner/A", "gh_valid": "True", "class": "A"},
        {"github_repo": "owner/b", "gh_valid": "True", "class": "B"},
        {"github_repo": "owner/c", "gh_valid": "True", "class": "C"},
        {"github_repo": "owner/dead", "gh_valid": "False", "class": "A"},
        {"github_repo": "", "gh_valid": "True", "class": "A"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/a", "valid": "True", "repo_id": "11", "archived": "False", "size": "5", "stars": "9"},
        {"repo": "owner/b", "valid": "True", "repo_id": "22", "archived": "True", "size": "1", "stars": "1"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_risk_repos(value_file=str(value), repos_file=str(gh))
    slugs = {e.repo for e in out}
    assert slugs == {"owner/a"}  # b archived, c not in classes, dead invalid, "" orphan
    assert out[0].repo_id == "11"
    assert out[0].value_class == "A"


def test_load_risk_repos_keeps_archived_when_flag_off(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "gh_valid", "class"], [
        {"github_repo": "owner/b", "gh_valid": "True", "class": "B"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/b", "valid": "True", "repo_id": "22", "archived": "True", "size": "1", "stars": "1"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_risk_repos(value_file=str(value), repos_file=str(gh), skip_archived=False)
    assert {e.repo for e in out} == {"owner/b"}


def test_load_repo_ids(tmp_path):
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id"], [
        {"repo": "Owner/Name", "valid": "True", "repo_id": "99"},
    ])
    assert repos.load_repo_ids(repos_file=str(gh)) == {"owner/name": "99"}
```

- [ ] **Step 2: Run test, verify it fails** — `uv run pytest tests/test_repos_loader.py -q` → FAIL (`load_risk_repos` / `load_repo_ids` not defined).

- [ ] **Step 3: Implement** in `src/pipeline/repos.py`. Add at top: `from src.pipeline.params import RISK_INPUT_CLASSES`. Add `REPOS_FILE = "data/sources/github/repos.csv"`. Change `_RANK` to all classes: `_RANK = {"A": 4, "B": 3, "C": 2, "D": 1}`. Add:

```python
def _load_repos_meta(path: str) -> dict[str, RepoEntry]:
    """Map lowercased repo slug → RepoEntry enriched from data/sources/github/repos.csv."""
    out: dict[str, RepoEntry] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = RepoEntry(
                repo=slug,
                repo_id=(row.get("repo_id") or "").strip(),
                size_kb=int(row.get("size") or 0),
                stars=int(row.get("stars") or 0),
                archived=(row.get("archived") or "").strip().lower() in ("true", "1"),
                enriched=True,
            )
    return out


def load_risk_repos(
    value_file: str = VALUE_FILE,
    repos_file: str = REPOS_FILE,
    skip_archived: bool = True,
    skip_invalid: bool = True,
) -> list[RepoEntry]:
    """Return repos in the risk-input value classes, sorted by slug.

    The risk pipeline runs on this set: repos whose `class` in
    `value-data.csv` is one of `settings.json risk_input.value_classes`
    (default {A, B}).

    - Keeps rows with `class` in RISK_INPUT_CLASSES and a non-empty
      `github_repo`. `skip_invalid` drops `gh_valid` != True (404 repos).
    - Deduped by lowercased `github_repo`; highest class wins (A > B > C > D).
    - repo_id / archived / size_kb / stars enriched from `data/sources/github/repos.csv`.
      `skip_archived` drops archived repos.
    """
    chosen: dict[str, str] = {}
    with open(value_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cls = (row.get("class") or "").strip()
            if cls not in RISK_INPUT_CLASSES:
                continue
            slug = (row.get("github_repo") or "").strip().lower()
            if not slug:
                continue
            if skip_invalid and (row.get("gh_valid") or "").strip() != "True":
                continue
            if slug not in chosen or _RANK.get(cls, 0) > _RANK.get(chosen[slug], 0):
                chosen[slug] = cls

    meta = _load_repos_meta(repos_file)
    entries: list[RepoEntry] = []
    skipped_archived = 0
    for slug, cls in chosen.items():
        e = meta.get(slug) or RepoEntry(repo=slug)
        e.repo = slug
        e.value_class = cls
        if skip_archived and e.archived:
            skipped_archived += 1
            continue
        entries.append(e)
    if skipped_archived:
        log.info("Skipped %d archived risk repos", skipped_archived)
    entries.sort(key=lambda e: e.repo)
    return entries


def load_risk_slugs(*args, **kwargs) -> list[str]:
    """Convenience wrapper returning just the lowercased repo slugs."""
    return [e.repo for e in load_risk_repos(*args, **kwargs)]


def load_repo_ids(repos_file: str = REPOS_FILE) -> dict[str, str]:
    """Map lowercased repo slug → repo_id from data/sources/github/repos.csv."""
    out: dict[str, str] = {}
    if not os.path.exists(repos_file):
        return out
    with open(repos_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            rid = (row.get("repo_id") or "").strip()
            if slug and rid:
                out[slug] = rid
    return out


# Back-compat aliases — risk scripts migrate to load_risk_*; these keep
# any remaining `load_ab_*` imports working. `top_repos_file` kwarg of the
# old load_ab_repos is no longer accepted.
load_ab_repos = load_risk_repos
load_ab_slugs = load_risk_slugs
```

Delete the old `load_ab_repos` / `load_ab_slugs` function bodies and the now-unused `_load_top_repos_meta` / `TOP_REPOS_FILE`. Keep `load_eligible_repos` untouched.

- [ ] **Step 4: Run test, verify pass** — `uv run pytest tests/test_repos_loader.py -q` → PASS (3 tests).
- [ ] **Step 5:** Run `uv run python -c "from src.pipeline.repos import load_risk_repos; r=load_risk_repos(); print(len(r), r[0].repo, r[0].repo_id, r[0].value_class)"` — Expected: ~900 repos, a valid slug + numeric id + A/B.
- [ ] **Step 6:** Commit: `feat: add settings-driven load_risk_repos shared loader`

---

## Phase 2 — Rewire risk scripts (parallelizable; one subagent per task)

For **every** task below the mechanical change is identical:
1. Import: `load_eligible_repos` → `load_risk_repos` (or `load_risk_slugs`).
2. Call sites: `load_eligible_repos()` → `load_risk_repos()`.
3. Any `repo→repo_id` map built by reading `data/eligibility/eligibility.csv` → use `load_repo_ids()` from `src.pipeline.repos` (reads `data/sources/github/repos.csv`). Delete the local `ELIGIBILITY*` constant + reader.
4. Reword "eligible" → "risk-scope" / "A/B value-class" in module docstring, `rich` banners, and `argparse` help. `--eligibility` CLI args become `--input` pointing at `value-data.csv` (keep the old flag name as a hidden alias only if already documented).
5. Smoke-test with the script's existing `--limit`/`--random` flag (N=3) — must run without error and report ~3 repos.
6. Commit per file or per small group.

`RepoEntry` from `load_risk_repos` exposes `.repo`, `.repo_id`, `.value_class`, `.size_kb`, `.stars`, `.archived` — a superset of what `load_eligible_repos` returned, so existing `entry.repo` / `entry.repo_id` usage is unchanged.

### Task 3: Rewire pipeline builders + aggregator

**Files (Modify):** `src/pipeline/build_concentration.py`, `build_complexity.py`, `build_funding.py`, `build_security.py`, `build_visibility.py`, `build_workload.py`, `risk.py`

- [ ] Apply the mechanical change to all 7. None of these build a repo_id map from eligibility-data.csv (they get repo_id from the `RepoEntry`) — only docstring "data/eligibility/eligibility.csv — eligible set" lines need updating to "data/value/value.csv — A/B value-class set".
- [ ] `risk.py`: also update `--random` help and the two banner strings ("eligible repos" → "risk-scope repos").
- [ ] Smoke-test each: `uv run python -m src.pipeline.build_concentration` etc. need their intermediates — instead run `uv run python -c "from src.pipeline.build_concentration import *"` import-check, and for `risk.py` run `uv run python -m src.pipeline.risk --random 3`.
- [ ] Commit: `refactor: pipeline builders read risk-scope (A/B value classes)`

### Task 4: Rewire github fetchers (loader-only)

**Files (Modify):** `src/github/fetch_churn.py`, `fetch_semgrep.py`, `fetch_funding.py`, `fetch_contributors_metrics.py`

- [ ] `fetch_churn.py`, `fetch_semgrep.py`, `fetch_funding.py`: mechanical loader swap (no repo_id-from-eligibility map in these).
- [ ] `fetch_contributors_metrics.py`: already imports `load_ab_slugs` — change to `load_risk_slugs`.
- [ ] Smoke-test each with `--limit 3` / `--random 3`.
- [ ] Commit: `refactor: github metric fetchers read risk-scope`

### Task 5: Rewire github fetchers with repo_id maps

**Files (Modify):** `src/github/fetch_cognitive.py`, `fetch_advanced_complexity.py`, `fetch_issue_metrics.py`

- [ ] `fetch_cognitive.py`, `fetch_advanced_complexity.py`: loader swap + replace `ELIGIBILITY_FILE` repo_id usage. They build `repo_ids = {e.repo: e.repo_id for e in eligible}` from the loader entries — that still works (entries carry `repo_id`); just delete the unused `ELIGIBILITY_FILE` constant.
- [ ] `fetch_issue_metrics.py`: `load_ab_slugs` → `load_risk_slugs`; replace `_load_repo_ids(ELIGIBILITY_FILE)` with `load_repo_ids()` from `src.pipeline.repos`; drop `ELIGIBILITY_FILE` and the local `_load_repo_ids`; update `--eligibility` arg → `--input` default `data/value/value.csv`.
- [ ] Smoke-test each with `--limit 3` / `--random 3`.
- [ ] Commit: `refactor: github fetchers use repos.csv for repo_id`

### Task 6: Rewire git fetchers

**Files (Modify):** `src/git/fetch_scc.py`, `src/git/commits_years.py`, `src/git/resolve_head.py`

- [ ] All three: import `load_risk_repos` (drop `load_eligible_repos` and, in fetch_scc/commits_years, the now-redundant separate `load_ab_repos` import — use `load_risk_repos` for both the fetch list and size data).
- [ ] `fetch_scc.py`: replace the `_repo_id_map()` that reads `data/eligibility/eligibility.csv` with `load_repo_ids()`.
- [ ] Update the comments at `commits_years.py:318-321` and `fetch_scc.py:438` that explain "use the eligibility set" → "use the risk-scope set".
- [ ] Smoke-test each with its smoke-test flag (N=3).
- [ ] Commit: `refactor: git fetchers read risk-scope`

### Task 7: Rewire osv / depsdev / ossinsight / openssf fetchers

**Files (Modify):** `src/osv/fetch_cves.py`, `src/depsdev/fetch.py`, `src/ossinsight/fetch.py`, `src/openssf/scorecard.py`

- [ ] `fetch_cves.py`, `depsdev/fetch.py`, `ossinsight/fetch.py`: loader swap. These read `eligibility-data.csv` mainly for the repo list (+ repo_id) — entries from `load_risk_repos` carry `repo_id`, so use that; delete eligibility-file constants.
- [ ] `openssf/scorecard.py`: replace `load_repos_from_file`/`load_repo_ids` reading `ELIGIBILITY_CSV` — repo list from `load_risk_slugs()`, repo_id from `load_repo_ids()` (both from `src.pipeline.repos`). Drop `ELIGIBILITY_CSV`.
- [ ] Smoke-test each with `--limit 3` / `--random 3`.
- [ ] Commit: `refactor: osv/depsdev/ossinsight/openssf read risk-scope`

---

## Phase 3 — Value pipeline + docs

### Task 8: `value.py` keeps all classes

**Files (Modify):** `src/pipeline/value.py`

- [ ] Change `aggregate_by_repo` signature default `drop_d_class: bool = True` → `drop_d_class: bool = False`.
- [ ] Update the docstring of `aggregate_by_repo` and the comment block at lines ~352-357 ("value-data.csv stores only ABC-class repos") → "value-data.csv stores all classes A/B/C/D".
- [ ] Update the module docstring line ~27-28 ("≤90% C, rest D" already correct; remove any "only ABC" claim).
- [ ] Verify call site `value.py:942` `aggregate_by_repo(all_rows)` now keeps D.
- [ ] Run `uv run python -m src.pipeline.value` then `uv run python -c "import csv,collections;print(collections.Counter(r['class'] for r in csv.DictReader(open('data/value/value.csv'))))"` — Expected: A/B/C **and D** present.
- [ ] Commit: `feat: value-data.csv includes all classes (A/B/C/D)`

### Task 9: Update docs

**Files (Modify):** `docs/pipeline.md`, `docs/risk.md`, `docs/value.md`, `docs/eligibility.md`

- [ ] `docs/pipeline.md`: pipeline list + "Dataflow at a glance" diagram + funnel table + "How to refresh" order → **Value → Risk → Eligibility**. Risk input = `value-data.csv` A/B. Eligibility now after Risk.
- [ ] `docs/risk.md`: the Scripts-table row "Input is `eligibility-data.csv`" → "Input is `value-data.csv` (`class ∈ settings.risk_input.value_classes`, default A/B)". `params.json` → `settings.json`. Replace "eligible repos" framing with "risk-scope (A/B) repos". Update the source-file coverage intro count.
- [ ] `docs/value.md`: `params.json` → `settings.json`; note `value-data.csv` now carries all classes incl. D; pipeline-order mention.
- [ ] `docs/eligibility.md`: add a note that Eligibility runs **after** Risk and (target state) narrows to `value_class=A` ∩ highest risk class. Keep existing eligibility mechanics text.
- [ ] Commit: `docs: pipeline reorder Value→Risk→Eligibility`

---

## Phase 4 — Run the pipeline & fix anomalies

### Task 10: Run value pipeline

- [ ] `uv run python -m src.pipeline.value` — capture timing + class distribution.

### Task 11: Run builders + aggregator on existing data (gap report)

- [ ] Run all 6 `build_*.py` then `risk.py`. The `risk.py` coverage table shows exact per-column gaps on the new A/B scope. Record perf (elapsed, rows).

### Task 12: Collect missing data

- [ ] Run each fetcher (TTL-aware — only fetches gaps): `commits_years` → `resolve_head` → `fetch_contributors_metrics` → `fetch_issue_metrics` → `fetch_scc` → `fetch_cognitive`/`fetch_advanced_complexity` → `fetch_semgrep` → `fetch_churn` → `osv/fetch_cves` → `openssf/scorecard` → `depsdev/fetch` → `fetch_funding` → `ossinsight/fetch`. Run each at full scope (user-authorised). Capture items/sec + elapsed + counts per fetcher.
- [ ] Re-run Task 11 builders + `risk.py`.

### Task 13: Anomaly sweep + bug fixes

- [ ] For every fetcher/builder: inspect error counts, repos with empty metrics, suspicious values (negative LOC, HHI > 10000, close_ratio > 1, repo_id mismatches, dup rows). For each anomaly: find root cause, fix the bug, add a regression test under `tests/`, re-run the affected stage.
- [ ] `uv run pytest` — all green.
- [ ] Commit fixes individually with focused messages.

### Task 14: Final report

- [ ] Re-aggregate `risk-data.csv`. Produce a summary: per-stage perf stats, coverage table, anomalies found + fixed, final row/column counts.

---

## Self-review notes

- Spec §1 config → Task 1. §2 loader → Task 2. §3 rewire → Tasks 3-7. §4 value all-classes → Task 8. §5 docs → Task 9. §6 run plan → Tasks 10-14. All spec sections covered.
- `load_risk_repos` / `load_risk_slugs` / `load_repo_ids` signatures are defined once in Task 2 and referenced consistently in Tasks 3-7.
- Back-compat aliases `load_ab_repos`/`load_ab_slugs` defined in Task 2 protect any call site missed during rewiring.
