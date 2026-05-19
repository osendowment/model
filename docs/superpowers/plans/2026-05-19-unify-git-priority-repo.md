# Git-Priority Repo Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `git` URL authoritative over the `github_repo` field in `unify_value_data`, fixing the wrong `github_repo` values in `value-data.csv`, then regenerate the value→eligibility→risk cascade.

**Architecture:** `_select_group_github_repo` returns the GitHub slug parsed from the group's `git` URL whenever one exists; the `github_repo` field is a fallback only. The curated `value-repo-overrides.csv` (already wired into `aggregate_by_repo`) corrects the cases where the git URL is itself stale/fork/garbage. A full pipeline re-run propagates the change.

**Tech Stack:** Python, `uv`, pytest, `gh` CLI.

**Spec:** `docs/superpowers/specs/2026-05-19-unify-git-priority-repo-design.md`

---

### Task 1: Git-priority `_select_group_github_repo`

**Files:**
- Modify: `src/pipeline/value/unify_value_data.py` (`_select_group_github_repo`, ~line 242)
- Test: `tests/test_unify_value_data.py` (`TestAggregateByRepo`, ~line 466)

- [ ] **Step 1: Replace the obsolete test with git-priority tests**

In `tests/test_unify_value_data.py`, **delete** `test_majority_github_repo_wins_over_minority` (~line 466-484) — it asserts the old majority-wins behaviour that this change deliberately overturns. Add, in `TestAggregateByRepo`:

```python
    def test_git_url_slug_is_authoritative_over_member_field(self):
        # The git URL (ecosyste.ms-sourced) wins over the github_repo field.
        # Real case: the `influxdb` package's github_repo field is wrong
        # (`simplejson/simplejson`) but its git URL correctly names
        # influxdb/influxdb-python.
        rows = [
            _pkg_row("influxdb", "pypi", github_repo="simplejson/simplejson",
                     git_url="https://github.com/influxdb/influxdb-python.git",
                     pagerank="1.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert len(aggs) == 1
        assert aggs[0]["github_repo"] == "influxdb/influxdb-python"

    def test_git_url_slug_beats_member_field_majority(self):
        # Even a unanimous github_repo field loses to a usable git URL slug.
        # (typeshed's stub packages name python/typeshed but their git URL
        # is the stub_uploader repo; the URL wins here — value-repo-
        # overrides.csv is what restores python/typeshed in production.)
        rows = [_pkg_row(f"types-{i}", "pypi", github_repo="python/typeshed",
                         git_url="https://github.com/typeshed-internal/stub_uploader.git",
                         pagerank="1.0")
                for i in range(5)]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert len(aggs) == 1
        assert aggs[0]["github_repo"] == "typeshed-internal/stub_uploader"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_unify_value_data.py -k 'git_url_slug' -v`
Expected: both FAIL — the old code returns the member-field majority (`simplejson/simplejson`, `python/typeshed`).

- [ ] **Step 3: Implement git-priority in `_select_group_github_repo`**

Replace the body of `_select_group_github_repo` in `src/pipeline/value/unify_value_data.py`. Keep the signature `(_select_group_github_repo(members: list[dict], git_url: str) -> str)`. New body and docstring:

```python
def _select_group_github_repo(members: list[dict], git_url: str) -> str:
    """Pick the group's `github_repo`.

    The `git` URL is authoritative: when it yields a parseable GitHub slug
    (reserved namespaces filtered by `_github_repo_from_url`), that slug is
    the repo identity. The `github_repo` field is only a fallback, for
    groups whose `git` URL is a non-GitHub host or absent (orphans).

    Rationale: `git` is backfilled from ecosyste.ms, a purpose-built
    package→repository service; the `github_repo` field is a weaker guess
    that is sometimes flat wrong (e.g. the `influxdb` package tagged
    `simplejson/simplejson`). Cases where the git URL is itself stale or
    points at a fork are corrected by `value-repo-overrides.csv`, applied
    later in `aggregate_by_repo` — not patched here.

    Fallback (no usable GitHub URL): the most common `github_repo` among
    members, alphabetic tie-break.
    """
    url_slug = _github_repo_from_url(git_url)
    if url_slug:
        return url_slug
    member_repos = [m["github_repo"] for m in members if m.get("github_repo")]
    if not member_repos:
        return ""
    counts = Counter(member_repos)
    top_count = counts.most_common(1)[0][1]
    return min(slug for slug, c in counts.items() if c == top_count)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_unify_value_data.py -k 'git_url_slug' -v`
Expected: both PASS.

- [ ] **Step 5: Run the full unify test file to confirm no regressions**

Run: `uv run pytest tests/test_unify_value_data.py -v`
Expected: all PASS. The four other `_select_group_github_repo`-related tests still pass by construction: `test_github_repo_first_nonempty_member_wins` (URL slug == field), `test_mismatched_github_repo_does_not_collide_with_sibling_repo` (each group's URL slug is distinct), `test_keeps_member_github_repo_when_git_column_is_wrong` and `test_sponsors_url_with_two_members_does_not_pick_sponsor_slug` (the `sponsors/` URL is filtered → no usable slug → field fallback).

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/value/unify_value_data.py tests/test_unify_value_data.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "unify_value_data: git URL is authoritative for repo identity

_select_group_github_repo now returns the GitHub slug from the group's
git URL whenever one exists; the github_repo field is a fallback only.
The git URL is ecosyste.ms-sourced and reliable, whereas the github_repo
field is a weaker guess sometimes pointing at an unrelated repo. Cases
where the git URL is itself stale/fork are handled by the curated
value-repo-overrides.csv layer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Curate `value-repo-overrides.csv`

**Files:**
- Modify: `data/value-repo-overrides.csv` (append rows; header `package,ecosystem,github_repo,reason` already present, currently 1 row)

The git-priority rule relabels 23 repo groups. 4 are correct fixes (no action). 8 are confirmed regressions (add override rows). 11 need a GitHub check.

- [ ] **Step 1: Append the 8 confirmed-regression override rows**

Append these rows to `data/value-repo-overrides.csv` (one representative package per group — `apply_repo_overrides` relabels the whole group from any one member, keyed by `_group_key`):

```csv
filelock,pypi,tox-dev/filelock,"git URL is the pre-rename name tox-dev/py-filelock; the repo was renamed to tox-dev/filelock"
tomlkit,pypi,python-poetry/tomlkit,"git URL names the original-author repo sdispater/tomlkit; the project moved to the python-poetry org"
crashtest,pypi,python-poetry/crashtest,"git URL names sdispater/crashtest; the project moved to the python-poetry org"
pendulum,pypi,python-pendulum/pendulum,"git URL names sdispater/pendulum; the project moved to the python-pendulum org"
deepdiff,pypi,seperman/deepdiff,"git URL points at the fork qlustered/deepdiff; the canonical repo is seperman/deepdiff"
sphinxcontrib-httpdomain,pypi,sphinx-contrib/httpdomain,"git URL is the placeholder github.com/me/spam; the real repo is sphinx-contrib/httpdomain"
types-aiofiles,pypi,python/typeshed,"git URL points at typeshed-internal/stub_uploader (the stub-upload tool); the 38 types-* stub packages are sourced from python/typeshed"
microsoft-kiota-abstractions,pypi,microsoft/kiota-python,"git URL names the multi-language generator repo microsoft/kiota; the Python kiota libraries live in microsoft/kiota-python"
```

- [ ] **Step 2: Verify the 11 ambiguous relabels against GitHub**

For each `(package, ecosystem, old_field_slug → new_url_slug)` below, run:

```bash
gh api "repos/<new_url_slug>" --jq '{full_name, fork, archived}' 2>/dev/null || echo "404"
gh api "repos/<old_field_slug>" --jq '{full_name, fork, archived}' 2>/dev/null || echo "404"
```

Decision rule — **add an override row `package,ecosystem,<old_field_slug>,"<reason>"` iff** the new URL slug is a fork (`fork: true`), OR it 404s, OR its `full_name` differs from `<new_url_slug>` (a rename/redirect) while `<old_field_slug>` resolves to a non-fork repo. Otherwise keep the URL slug (no row).

The 11 cases:
```
dulwich,pypi             jelmer/dulwich → dulwich/dulwich
python-json-logger,pypi  madzak/python-json-logger → nhairs/python-json-logger
aiodns,pypi              aio-libs/aiodns → saghul/aiodns
oauth2client,pypi        googleapis/oauth2client → google/oauth2client
webencodings,pypi        gsnedders/python-webencodings → simonsapin/python-webencodings
pickleshare,pypi         ipython/pickleshare → pickleshare/pickleshare
ncurses,cpp              thomasdickey/ncurses-snapshots → mirror/ncurses
patchelf,pypi            nixos/patchelf → mayeut/patchelf-pypi
realtime,pypi            supabase/supabase-py → supabase/supabase
linux,cpp                archlinux/linux → torvalds/linux
python,cpp               10der/homeassistant-custom_components-awtrix → python/cpython
```

- [ ] **Step 3: Verify the override file parses**

Run: `uv run python -c "from src.pipeline.value.unify_value_data import load_repo_overrides; o=load_repo_overrides(); print(len(o),'overrides'); assert ('filelock','pypi') in o and ('types-aiofiles','pypi') in o; print('ok')"`
Expected: prints the count and `ok`.

- [ ] **Step 4: Run the override tests**

Run: `uv run pytest tests/test_unify_value_data.py -k 'Override or override' -v`
Expected: all PASS (the override mechanism is unchanged; only data added).

- [ ] **Step 5: Commit**

```bash
git add data/value-repo-overrides.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "data: curate value-repo-overrides for git-priority regressions

The git-priority rule relabels 23 repo groups; these override rows
restore the correct repo for the cases where the git URL is itself
stale (renamed repo), a fork, or a placeholder — leaving the genuine
fixes (e.g. influxdb → influxdb/influxdb-python) untouched.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Regenerate the value→eligibility→risk cascade

**Files:**
- Regenerated: `data/value-data.csv`, `data/eligibility-data.csv`, `data/{complexity,concentration,funding,security,visibility,workload}.csv`, `data/risk-data.csv`

- [ ] **Step 1: Snapshot the pre-change state for impact measurement**

```bash
cp data/value-data.csv /tmp/value-data.before.csv
cp data/risk-data.csv /tmp/risk-data.before.csv
```

- [ ] **Step 2: Regenerate value-data.csv and verify the duplicates are gone**

Run: `uv run python -m src.pipeline.value.unify_value_data`
Then verify the simplejson cluster collapsed:

```bash
uv run python -c "
import csv
from collections import Counter
rows=list(csv.DictReader(open('data/value-data.csv')))
dups={k:v for k,v in Counter((r['github_repo'] or '').strip().lower() for r in rows if (r['github_repo'] or '').strip()).items() if v>1}
print('duplicate github_repo:', dups)
"
```
Expected: `simplejson/simplejson` no longer duplicated (it drops from x4 to x1). Remaining entries, if any, are the separate orphan-split issue (out of scope).

- [ ] **Step 3: Re-verify git URLs**

Run: `uv run python -m src.pipeline.value.verify_git_urls`
Expected: completes; re-derives `gh_valid` / `gh_repo_id` for the changed repos (cached via `data/git/urls.csv`, so only new URLs are queried).

- [ ] **Step 4: Regenerate eligibility**

Run: `uv run python -m src.pipeline.eligibility.classify_eligibility`
Expected: writes `data/eligibility-data.csv`; prints the eligibility summary table.

- [ ] **Step 5: Regenerate the risk pipeline**

Run: `uv run python -m src.pipeline.run_risk_pipeline`
Expected: runs the 6 dimension builders + aggregate; ends with `risk-data.csv` written. (Builders only — no fetchers; `run_risk_pipeline` with no flags skips the fetch stage.)

- [ ] **Step 6: Run the health checks**

Run: `uv run python scripts/pipeline_health.py --strict && uv run python scripts/data_anomalies.py --strict`
Expected: both exit 0 — all pipeline_health checks pass, `data_anomalies` reports 0 err / 0 warn.

- [ ] **Step 7: Measure and record the impact**

```bash
uv run python -c "
import csv
def repos(p,col='repo'): return {r[col] for r in csv.DictReader(open(p))}
vb=repos('/tmp/value-data.before.csv','github_repo'); va=repos('data/value-data.csv','github_repo')
print('value-data github_repo — added:', len(va-vb), 'removed:', len(vb-va))
rb={r['repo'] for r in csv.DictReader(open('/tmp/risk-data.before.csv'))}
ra={r['repo'] for r in csv.DictReader(open('data/risk-data.csv'))}
print('risk scope — entered:', sorted(ra-rb), 'left:', sorted(rb-ra))
"
```
Record the output in the commit message.

- [ ] **Step 8: Commit the regenerated data**

```bash
git add data/value-data.csv data/eligibility-data.csv data/complexity.csv data/concentration.csv data/funding.csv data/security.csv data/visibility.csv data/risk-data.csv data/workload.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "data: regenerate value→eligibility→risk after git-priority repo fix

<paste the Step 7 impact summary here>

pipeline_health and data_anomalies both pass.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementer

- Do **not** change `_group_key` — orphan-split duplicates are explicitly out of scope (see spec).
- `data/funding.csv` may already show as modified from before this work; only `git add` the files listed in each task's commit step — never `git add -A`.
- The override file uses `csv.QUOTE_MINIMAL`-compatible rows; the `reason` field contains commas so it must stay double-quoted (as shown).
