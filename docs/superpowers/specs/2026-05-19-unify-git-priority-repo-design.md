# Unify: git-priority repo resolution — design

**Date:** 2026-05-19
**Status:** awaiting user review
**Scope:** `src/pipeline/value/unify_value_data.py` repo identification, then a
full value→eligibility→risk regeneration cascade.

## Problem

`data/{eco}/results.csv` carries two repo signals per package, from two
different resolvers:

- **`github_repo`** — written by `src/pypi/process_data.py`'s `github_map`
  (a guess source — sometimes flat wrong, e.g. the `influxdb` package tagged
  `simplejson/simplejson`, an unrelated repo).
- **`git`** — backfilled by `src/ecosystems/packages.py` from ecosyste.ms,
  a purpose-built package→repository service.

When the two disagree, `unify_value_data._select_group_github_repo` currently
picks the most-common `github_repo` field (URL wins only on a tie). That lets
a wrong field value label the repo — producing 7 duplicate-`github_repo` rows
in `value-data.csv` (the table is meant to be one row per repo).

## Decision

**The `git` URL is the authoritative source.** When a group's `git` URL
yields a parseable GitHub slug, that slug is the repo identity — it outranks
the `github_repo` field unconditionally.

This is deliberately a simple, deterministic rule rather than a heuristic.
Its known failure modes (the `git` URL being stale after a rename, or
pointing at a fork/wrapper) are handled by the **curated override layer**,
not by complicating the rule.

## Approach

### 1. `_select_group_github_repo` — git-priority

```
url_slug = _github_repo_from_url(git_url)
if url_slug:
    return url_slug                      # git URL is authoritative
# no usable GitHub URL (non-GitHub host, or orphan group) — fall back
# to the most-common github_repo field, alphabetic tie-break
member_repos = [m["github_repo"] for m in members if m.get("github_repo")]
if not member_repos:
    return ""
counts = Counter(member_repos)
top = counts.most_common(1)[0][1]
return min(s for s, c in counts.items() if c == top)
```

`_github_repo_from_url` already filters GitHub reserved namespaces
(`sponsors/`, `orgs/`, …), so org/discovery URLs do not become slugs.

### 2. Override layer (already built — `value-repo-overrides.csv`)

`load_repo_overrides` / `apply_repo_overrides` already exist and run as the
last step of `aggregate_by_repo`. They force the correct `github_repo` /
`git_url` for listed `(package, ecosystem)` pairs. No code change needed —
the work is **curating the entries**.

### 3. Override curation

The git-priority rule relabels **23 repo groups** (full dry-run list below).
Each must get a verdict:

- **Keep** the new URL-derived label — the field was the wrong one.
- **Override** — the URL is itself wrong; add a `value-repo-overrides.csv`
  row restoring the correct repo.

Verdict method, per relabel (4 fixes + 8 regressions + 11 verify = 23):
1. The 4 confirmed fixes — keep (no override).
2. The 8 confirmed regressions — add override rows.
3. The 11 "verify" cases — check each against GitHub (`data/github/repos.csv`
   `full_name` resolves renames; the package's own registry identity
   confirms which repo it belongs to). Regression → override; otherwise keep.

**The 23 relabels** (`old -> new`):

Confirmed fixes (keep — field was an unrelated wrong guess):
- `simplejson/simplejson` → `influxdb/influxdb-python`
- `simplejson/simplejson` → `areski/python-nvd3`
- `simplejson/simplejson` → `lucretiel/autocommand`
- `georgmartius/vid` → `georgmartius/vid.stab` (field truncated)

Confirmed regressions (override — git URL stale/fork/garbage):
- `tox-dev/filelock` ← `tox-dev/py-filelock` (stale rename)
- `python-poetry/tomlkit` ← `sdispater/tomlkit` (org transfer)
- `python-poetry/crashtest` ← `sdispater/crashtest` (org transfer)
- `python-pendulum/pendulum` ← `sdispater/pendulum` (org transfer)
- `seperman/deepdiff` ← `qlustered/deepdiff` (fork)
- `sphinx-contrib/httpdomain` ← `me/spam` (placeholder URL)
- `python/typeshed` ← `typeshed-internal/stub_uploader` (38 pkgs; the
  stub-upload tool, not the project)
- `microsoft/kiota-python` ← `microsoft/kiota` (per-language repo split)

Verify against GitHub (keep or override per finding):
- `jelmer/dulwich` → `dulwich/dulwich`
- `madzak/python-json-logger` → `nhairs/python-json-logger`
- `aio-libs/aiodns` → `saghul/aiodns`
- `googleapis/oauth2client` → `google/oauth2client`
- `gsnedders/python-webencodings` → `simonsapin/python-webencodings`
- `ipython/pickleshare` → `pickleshare/pickleshare`
- `thomasdickey/ncurses-snapshots` → `mirror/ncurses`
- `nixos/patchelf` → `mayeut/patchelf-pypi`
- `supabase/supabase-py` → `supabase/supabase`
- `archlinux/linux` → `torvalds/linux`
- `10der/homeassistant-custom_components-awtrix` → `python/cpython`

### 4. Regeneration cascade

After the code + override changes:
1. `unify_value_data` → `data/value-data.csv`
2. `verify_git_urls` → re-derives `gh_valid` / `gh_repo_id` for changed repos
3. `classify_eligibility` → `data/eligibility-data.csv`
4. `run_risk_pipeline` (builders + aggregate) → all risk CSVs + `risk-data.csv`

Then `scripts/pipeline_health.py` + `scripts/data_anomalies.py` must pass,
and the impact (repos entering/leaving risk scope, class shifts) is measured
and reported.

## Testing

- Unit: `_select_group_github_repo` returns the URL slug when a GitHub
  `git_url` is present (overriding the member-field majority); falls back to
  the field only with no usable URL. Update any existing test that asserted
  the old majority-wins behaviour.
- Data assertion: after the run, `value-data.csv` has **0** duplicate
  `github_repo` values among single-github rows that the rule resolves
  (the simplejson cluster collapses; remaining dups, if any, are the
  separate orphan-split issue — see Out of scope).
- The shipped `value-repo-overrides.csv` parses and contains every curated
  entry; existing override tests continue to pass.

## Out of scope

**Orphan-split duplicates.** Packages with a `github_repo` but no `git` URL
become per-package orphan rows; a repo whose packages split across "has-URL"
and "no-URL" can still appear as 2+ rows. Fixing that needs a canonical
group key reconciling both identities (a chicken-and-egg with per-group
label resolution) — a separate design. This spec does not change `_group_key`.

## Files touched

- `src/pipeline/value/unify_value_data.py` — `_select_group_github_repo`.
- `data/value-repo-overrides.csv` — curated regression entries.
- `tests/test_unify_value_data.py` — updated/added tests.
- Regenerated data: `value-data.csv`, `eligibility-data.csv`, the risk CSVs,
  `risk-data.csv` (cascade).
