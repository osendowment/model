# Validation Table

`data/value/validation.csv` is the **git/GitHub validation audit table** — a
rollup of the two per-source validation caches that decides, per validation
*target*, whether a repo's resolved URL is real and reachable. It is the audit
trail behind the `git_valid` column in
[`data/value/value.csv`](../value.md): `validation.csv` records *why* each target
was judged valid (which cache verdict, checked when, which ecosystems point at
it), and `build_validation` joins that verdict back onto every value row.

**Grain:** one row per distinct *validation target*, not per repo. A target is
either a GitHub `owner/repo` slug (type `github_repo`) or a non-GitHub clone URL
(type `git_url`). Many value rows can share a target, and they union their
ecosystems into the target's `sources` column.

## How it's built

Built by [`src/value/build_validation.py`](../../src/value/build_validation.py),
which runs as the `validation` step of the value pipeline runner (after
`unify`, before the final `criticality` step). It does one piece of network
I/O of its own: before rolling up, it refreshes the non-GitHub `git ls-remote`
reachability cache (`data/sources/git/urls.csv`, TTL 365 days) — `--offline`
skips the refresh (cache only), `--refresh` forces a re-check regardless of
age. The GitHub cache (`data/sources/github/repos.csv`) is not touched here:
it is the GitHub Repos API record maintained by
`src.sources.github.fetch_repo_owner_data`, refreshed during the rollup's
earlier `resolve` step.

Steps:

1. **Collect targets** from `data/value/value.csv`. For each value row,
   `_row_target` picks a single target: the row's `repo` slug (type
   `github_repo`) when its `platform == github`, else its canonicalised
   non-GitHub `git_url` (type `git_url`). The GitHub branch wins, so a GitHub row's derived
   `git_url` is never double-counted. A row with neither is an **orphan** and
   contributes no target. Each target accumulates the `sources` — the ecosystems
   (from each row's `ecosystems` column) whose packages resolve to it.
2. **Load verdicts** from the two caches:
   - `data/sources/github/repos.csv` — `valid` + `fetched_at`, keyed by **both**
     the queried `repo` slug and the rename-resolved `full_name` (so a value row
     holding either form resolves). `valid` is parsed case-insensitively.
   - `data/sources/git/urls.csv` — `valid` + `checked_at`, keyed by `url`.
3. **Apply override pins** from `data/value/overrides.csv`. A row there may pin a
   target's validity via its `valid` column (`True`/`False`); the pin resolves
   to the override's `repo` target, else its `git_url` target, and
   **overrides whatever the cache said** (its `checked_at` is recorded as the
   literal string `override`). A pin with no resolvable target is skipped with a
   warning.
4. **Hard gate:** every collected target must have a verdict. If any target has
   none, the step raises `SystemExit` listing the offenders and refusing to
   write — a missing verdict is treated as a pipeline error, never silently
   invalid (see *Refreshing* below).
5. **Write** `validation.csv` (sorted by `type`, then `target`) and **join** the
   per-target verdict back into the `git_valid` column of `value.csv`.

## The `valid` / `git_valid` columns

`build_validation` produces two values from the same verdicts: a per-*target*
`valid` in `validation.csv`, and a per-*row* (per-repo) `git_valid` joined
into `value.csv`.

In **`validation.csv`**, `valid` is a plain boolean (`True`/`False`) — the cache
verdict (or override pin) for that one target.

In **`value.csv`**, `git_valid` is a boolean set by `join_valid` from each
row's target verdict. It is **host-agnostic**: a reachable non-GitHub upstream
(sourceware / savannah / a GitLab host / …) is valid on its own, not only a
GitHub repo.

| `git_valid` | Meaning | How derived |
|---------|---------|-------------|
| `True`  | The repo's upstream is real/reachable. | The row's target verdict is `True` (GitHub Repos API for `platform == github` rows, `git ls-remote` for non-GitHub `git_url` rows) — **or** the row carries a GitLab `gl/` `repo_id`, which is itself a validity proof: the resolver only assigns it after the GitLab project API confirmed the project exists, more authoritative than `git ls-remote` (which can fail on hosts like salsa.debian.org even for live projects). This keeps the `repo_id ⇒ git_valid` invariant. |
| `False` | No reachable upstream. | Orphan rows (no target at all — nothing to validate), a target whose reachability check failed, or a github `repo` that 404s. |

Marking a non-GitHub upstream valid does **not** pull it into the risk /
eligibility scope — `load_top_repos` still filters to the platforms configured
in `settings.json → top_repos`; `git_valid` only records that the URL
resolves.

So `validation.csv` is the per-target ledger and `value.csv`'s `git_valid` is
its per-row projection. Every non-orphan `value.csv` row's `git_valid` traces
to its one `validation.csv` row (matched by the row's target + type) — except
`gl/`-id rows, which are `True` regardless of their `git_url` target's
ls-remote verdict.

## Columns

| Column | Description |
|--------|-------------|
| `target` | The validated identity: a lowercase GitHub `owner/repo` slug, or a non-GitHub clone URL. |
| `type` | `github_repo` (validated via the GitHub Repos API) or `git_url` (validated via `git ls-remote`). |
| `sources` | Comma-separated, sorted list of ecosystems (`npm`, `pypi`, `crates`, `cpp`) whose packages resolve to this target. |
| `checked_at` | When the verdict was produced: the cache's `fetched_at` (GitHub) / `checked_at` (git), or the literal `override` when pinned via `overrides.csv`. |
| `valid` | `True`/`False` — whether the target was found real/reachable. |

## Refreshing

`build_validation` refreshes the non-GitHub `git ls-remote` cache itself
(TTL 365 days), then rolls up:

```bash
# Rebuild validation.csv and re-join git_valid onto value.csv.
# Refreshes stale ls-remote entries first (the step's only network I/O).
uv run python -m src.value.build_validation

# Variants:
uv run python -m src.value.build_validation --offline   # cache only, no network
uv run python -m src.value.build_validation --refresh   # force re-check all URLs
```

The GitHub cache (`data/sources/github/repos.csv`) is refreshed by the
rollup's `resolve` step, so to refresh everything run the rollup (or the whole
value pipeline), which wires the steps in order
(`eco-fetch` → `resolve` → `unify` → `validation` → `criticality`):

```bash
uv run python -m src.value.run_value_pipeline --rollup
```

> **Prerequisite:** `build_validation` will not invent verdicts. If a target in
> `value.csv` has no entry in either cache, the **hard gate** aborts the build
> with the message *"Run `uv run python -m src.value.run_value_pipeline
> --rollup` first."* — run that (or the full pipeline) to populate the caches,
> then rebuild.
