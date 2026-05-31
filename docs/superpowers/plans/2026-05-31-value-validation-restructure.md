# Implementation Plan: Value-stage validation restructure

## Goal

Replace the `gh_valid`/`git_valid`/`llm_guess` columns in `data/value/value.csv`
with a single tri-state `valid` column projected from a new aggregated audit
table `data/value/validation.csv`; rename + restructure manual overrides into
`data/value/overrides.csv` (adding `git_url` and `valid` override columns);
make validation a hard gate over the full value universe via a new
`build_validation` pipeline step.

## Design Reference

`docs/superpowers/specs/2026-05-31-value-validation-restructure-design.md`

## Prerequisites

- On branch `fix/unify-git-priority-repo` (current). Working tree clean.
- `data/sources/github/repos.csv` and `data/sources/git/urls.csv` are populated
  and fresh (just refreshed in commit `0e62e4b`).
- Full suite green at start: `uv run pytest -q` → 337 passed.

## Conventions

- Per-commit: each task is its own commit, author `kv@kvinogradov.com`
  (`git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit`).
- TDD where logic is non-trivial (Tasks 3, 4): write the failing test first.
- Run `uv run pytest -q` after each task; never commit red.
- Do NOT push.

## Tasks

---

### Task 1 — Rename + restructure overrides file

**Files:**
- `data/value/value-repo-overrides.csv` → `data/value/overrides.csv` (git mv)
- `src/pipeline/value/unify_value_data.py`
- `tests/test_unify_value_data.py`

**Description:**
1. `git mv data/value/value-repo-overrides.csv data/value/overrides.csv`.
2. Rewrite the header to `package,ecosystem,github_repo,git_url,valid,reason`
   and add the two new empty columns to all 14 existing rows (use a tiny
   Python rewrite, NOT manual edit; preserve existing values + quoting).
3. In `unify_value_data.py`:
   - `OVERRIDES_FILE = DATA_DIR / "value" / "overrides.csv"` (line 69).
   - `load_repo_overrides` (line 276): return
     `dict[tuple[str,str], dict]` where the value is
     `{"github_repo": slug, "git_url": url, "valid": pin}` (was just `slug`).
     Still require non-empty `reason`; skip rows missing it (log a warning).
     Keep keying on `(package, ecosystem)`.
   - `apply_repo_overrides` (line 297): when an override has `github_repo`,
     set `a["github_repo"]` + `a["git_url"] = _github_git_url(slug)` as today;
     when it has `git_url` instead (and no `github_repo`), set
     `a["git_url"] = url` and leave `github_repo` as-is. The `valid` pin is
     NOT applied here (it's consumed in Task 4 by build_validation) — but
     `load_repo_overrides` must surface it.
   - Update the docstrings/comments that name `value-repo-overrides.csv`
     (lines 254, 305, 445) to `overrides.csv`.
4. Update `tests/test_unify_value_data.py`: the override fixtures now build
   the new dict shape; rename the temp file to `overrides.csv`; the existing
   `test_override_applied_to_aggregate` / `test_override_changes_git_url_too`
   adapt to the new `load_repo_overrides` return type. Add a test that a
   `git_url`-only override sets the group's `git_url` without a `github_repo`.

**Pattern (loader return):**
```python
out[(pkg, eco)] = {
    "github_repo": _normalise_repo(r.get("github_repo") or ""),
    "git_url": (r.get("git_url") or "").strip().lower(),
    "valid": (r.get("valid") or "").strip(),
}
```

**Verification:**
- `data/value/overrides.csv` header is the 6-column form; 14 rows intact,
  `git_url`/`valid` blank.
- `uv run pytest tests/test_unify_value_data.py -q` → green.
- `git grep -n value-repo-overrides src tests` → no hits.

**Depends on:** none.

---

### Task 2 — Trim verify_git_urls: stop writing valid columns into value.csv

**Files:** `src/pipeline/value/verify_git_urls.py`

**Description:**
`verify_git_urls` keeps doing all network validation (canonicalise, fetch
GitHub repos into `repos.csv`, ls-remote non-github into `urls.csv`, rewrite
`github_repo` to current post-rename names) but **no longer writes**
`gh_valid` / `git_valid` into the rows, since those columns are removed from
the schema. `gh_repo_id` writing **stays** (identity, still in schema).

1. In `verify_urls_in_aggregates` (line 321) keep: canonicalisation, the
   github/non-github split, `fetch_and_persist`, `gh_meta` read-back,
   `_verify_non_github`, the `gh_repo_id` set (line 419), and the
   `github_repo` rename rewrite (lines 423-427).
2. Remove the `a["gh_valid"] = …` (416), `a["git_valid"] = …` (429), and the
   `invalid_examples` accumulation that depends on them (434-437). Keep the
   summary counts but recompute them from `gh_meta`/`nongh_valid` directly
   (valid/invalid/no-url) so the printed table still works without per-row
   `gh_valid` keys.
3. `main()` (line 493): still reads `value.csv`, calls
   `verify_urls_in_aggregates`, writes back via `write_value_data` (which now
   uses the trimmed FIELDS from Task 3 — so order Task 3 first OR keep
   `extrasaction="ignore"` which already drops unknown keys; FIELDS change is
   Task 3). Update the docstring (lines 1-23) to drop the
   `gh_valid`/`git_valid` description and point at `build_validation` for
   verdicts.

**Verification:**
- `uv run python -c "import src.pipeline.value.verify_git_urls"` imports clean.
- `git grep -n "gh_valid\|git_valid" src/pipeline/value/verify_git_urls.py`
  → no hits.
- `uv run pytest -q` → still green (no test asserts those keys directly).

**Depends on:** none (but commit after Task 3 if FIELDS ordering matters;
`extrasaction="ignore"` makes it safe either way).

---

### Task 3 — value.csv schema: drop 3 cols, add `valid`

**Files:**
- `src/pipeline/value/unify_value_data.py`
- `tests/test_unify_value_data.py`

**Description:**
1. `FIELDS` (line 74): remove `gh_valid`, `git_valid`, `llm_guess`; add
   `valid` after `git_url`. New tuple:
   ```python
   FIELDS = (
       ["id", "github_repo", "gh_repo_id", "git_url", "valid",
        "ecosystems", "packages",
        "top_eco", "top_eco_pkg", "top_eco_pct", "class"]
       + [f"class_{e}" for e in ECOSYSTEMS]
   )
   ```
2. Remove `llm_guess` from the per-package row dict (`collect_ecosystem`,
   ~line 155) and from the aggregate (`aggregate_by_repo`: the `llm_tags`
   union block ~lines 372-377, 387, and the `_INTERNAL_PREFIXES`/`ecosystems`
   are unaffected). `unify` writes `valid` as empty string (placeholder);
   `build_validation` fills it in Task 4.
3. Set `a["valid"] = ""` default when building each aggregate so the column
   exists even before build_validation runs.
4. Update module docstring (lines 13-23) for the new schema.
5. Tests: update any assertion of the old header; add a test that
   `write_value_data` output header == new FIELDS and contains no
   `gh_valid`/`git_valid`/`llm_guess`.

**Verification:**
- `uv run pytest tests/test_unify_value_data.py -q` → green.
- `uv run python -m src.pipeline.value.unify_value_data` then
  `head -1 data/value/value.csv` shows the new 15-col header with `valid`,
  no dropped cols.

**Depends on:** Task 1 (overrides loader shape).

---

### Task 4 — build_validation.py (new step) + validation.csv

**Files:**
- `src/pipeline/value/build_validation.py` (new)
- `tests/test_build_validation.py` (new)

**Description (TDD — write test first):**
New module. Public API:
- `VALIDATION_FILE = DATA_DIR / "value" / "validation.csv"`,
  fields `["target", "type", "sources", "checked_at", "valid"]`.
- `collect_targets(value_rows) -> dict[(target,type)] -> set[sources]` —
  for each value row, its github_repo target (type `github_repo`) and/or its
  non-github git_url target (type `git_url`); accumulate the row's
  `ecosystems` (comma-split) into the target's source set. Orphan rows
  (no target) contribute nothing.
- `load_verdicts() -> dict[(target,type)] -> (valid: bool, checked_at: str)` —
  read `data/sources/github/repos.csv` (key=`repo` lower, type github_repo,
  valid from `valid` col, checked_at from `fetched_at`) and
  `data/sources/git/urls.csv` (key=`url`, type git_url, valid+checked_at).
- `apply_overrides(verdicts, overrides)` — `overrides.csv` `valid` pins win:
  for a `(package,ecosystem)` override with a non-empty `valid`, resolve its
  target (github_repo slug or git_url) and force the verdict
  (`checked_at="override"`).
- `build(value_rows, verdicts, sources) -> (validation_rows, value_rows)`:
  - **Hard gate:** every collected target must be in `verdicts`. Collect the
    misses; if any, `raise SystemExit` with the list and the hint to run
    `verify_git_urls`. Never default missing→invalid.
  - Build `validation.csv` rows (sorted by target), `sources` = sorted
    comma-join.
  - Join `valid` into each value row: `True` if all its targets valid,
    `False` if any invalid, `""` if no target.
- `main()`: read `value.csv`, `collect_targets`, `load_verdicts`+overrides,
  `build`, write `validation.csv` (atomic, QUOTE_ALL), rewrite `value.csv`
  via `unify_value_data.write_value_data`, print a rich summary
  (valid/invalid/orphan counts + sources breakdown).

Tests in `tests/test_build_validation.py`:
- `collect_targets` splits github vs git, accumulates multi-eco sources.
- `build` row `valid` = AND of targets (True / False / "" orphan).
- override `valid` pin overrides a cache verdict.
- hard gate: a target absent from verdicts raises SystemExit naming it.

**Verification:**
- `uv run pytest tests/test_build_validation.py -q` → green.
- `uv run python -m src.pipeline.value.build_validation` →
  `data/value/validation.csv` exists with the 5-col header; `value.csv`
  `valid` column populated; counts roughly match the prior run
  (~10.4k valid, ~90 invalid, ~1.6k orphan).

**Depends on:** Task 3 (value.csv has `valid` col + `ecosystems`).

---

### Task 5 — Wire build_validation into the runner + fix risk loader

**Files:**
- `src/pipeline/run_value_pipeline.py`
- `src/pipeline/common/repos.py`
- `tests/test_repos_loader.py`

**Description:**
1. `run_value_pipeline.py` STEPS: add
   `Step("validation", "src.pipeline.value.build_validation")` after the
   `verify` step. (Also: the `downloads` step still names
   `build_ecosystem_downloads` — leave as-is; the stats.csv rename is a
   separate spec.)
2. `repos.py` line 114: the risk loader currently skips rows with
   `gh_valid != "True"`. Change to read the new column:
   ```python
   if skip_invalid and (row.get("valid") or "").strip() != "True":
       continue
   ```
   This keeps the risk pipeline filtering to validated repos (now via the
   unified `valid`), satisfying the design's risk-side contract. Update the
   surrounding docstring/comment that references `gh_valid`.
3. `tests/test_repos_loader.py`: replace `gh_valid` with `valid` in
   `_value_row` and every header list / fixture (lines 23, 45, and the
   per-test inline headers).

**Verification:**
- `uv run python -m src.pipeline.run_value_pipeline --list` shows the
  `validation` step after `verify`.
- `uv run pytest tests/test_repos_loader.py -q` → green.
- `git grep -n gh_valid src tests` → no hits.

**Depends on:** Tasks 3, 4.

---

### Task 6 — Regenerate data + docs

**Files:**
- `data/value/value.csv`, `data/value/validation.csv` (regenerated)
- `docs/value.md`, `docs/pipeline.md`, `CLAUDE.md`

**Description:**
1. Regenerate from existing artifacts (no ecosystem refetch):
   ```
   uv run python -m src.pipeline.value.unify_value_data
   uv run python -m src.pipeline.value.verify_git_urls
   uv run python -m src.pipeline.value.build_validation
   ```
   (verify uses fresh caches; no network beyond the ~0 stale targets.)
2. `docs/value.md` / `docs/pipeline.md`: document the `valid` column,
   `validation.csv` (cols + that it's a rollup), and `overrides.csv` (new
   cols). Remove `gh_valid`/`git_valid`/`llm_guess` mentions.
3. `CLAUDE.md` Data Organization → value stage: list `validation.csv` and
   `overrides.csv` (rename), note `value.csv` carries `valid`.

**Verification:**
- `head -1 data/value/value.csv` → new schema; `head -1 data/value/validation.csv`
  → `target,type,sources,checked_at,valid`.
- `git grep -n "gh_valid\|git_valid\|llm_guess\|value-repo-overrides" src scripts docs CLAUDE.md`
  → no hits (excluding the dated spec/plan files).

**Depends on:** Tasks 1-5.

---

## End-to-end verification

1. `uv run pytest -q` → all green (≥ 337 + new tests).
2. Full value-stage rebuild from existing data:
   `unify_value_data → verify_git_urls → build_validation` all exit 0.
3. `validation.csv` row count ≈ distinct (github + non-github) targets;
   spot-check a multi-ecosystem repo has `sources` like `npm,pypi`.
4. `value.csv` `valid` distribution sane (~10.4k True / ~90 False / ~1.6k
   empty), no `gh_valid`/`git_valid`/`llm_guess` columns.
5. Hard gate proven: temporarily remove one row from `repos.csv`, run
   `build_validation`, confirm it exits non-zero naming the target; restore.
6. `uv run python -m src.pipeline.common.repos` (or a risk builder) still
   loads the A/B set using the new `valid` column.

## Notes / out of scope

- stats.csv spec (`2026-05-31-ecosystem-stats-table-design.md`) is separate;
  the `downloads`→`stats` rename and `build_stats` are NOT in this plan.
- No new network fetching beyond what `verify_git_urls` already performs.
