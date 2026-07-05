# Validation Table

`data/value/validation.csv` is the **git/GitHub validation audit table** — a
rollup of the two per-source validation caches that decides, per validation
*target*, whether a repo's resolved URL is real and reachable. It is the audit
trail behind the tri-state `valid` column in
[`data/value/value.csv`](../value.md): `validation.csv` records *why* each target
was judged valid (which cache verdict, checked when, which ecosystems point at
it), and `build_validation` joins that verdict back onto every value row.

**Grain:** one row per distinct *validation target*, not per repo. A target is
either a GitHub `owner/repo` slug (type `github_repo`) or a non-GitHub clone URL
(type `git_url`). Many value rows can share a target, and they union their
ecosystems into the target's `sources` column.

## How it's built

Built by [`src/value/build_validation.py`](../../src/value/build_validation.py),
which runs as the `validation` step of the value pipeline runner (the last step,
after `unify` and `verify`). It is a **pure rollup — it does no network I/O**;
all verification happened earlier in `verify_git_urls`, which refreshes the two
source caches this step reads.

Steps:

1. **Collect targets** from `data/value/value.csv`. For each value row,
   `_row_target` picks a single target: the row's `repo` slug (type
   `github_repo`) when its `platform == github`, else its non-GitHub
   `git_url` (type `git_url`). The GitHub branch wins, so a GitHub row's derived
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
   per-target verdict back into the `valid` column of `value.csv`.

## The `valid` column

`build_validation` produces two `valid` values from the same verdicts: a
per-*target* `valid` in `validation.csv`, and a per-*row* (per-repo) `valid`
joined into `value.csv`.

In **`validation.csv`**, `valid` is a plain boolean (`True`/`False`) — the cache
verdict (or override pin) for that one target.

In **`value.csv`**, `valid` is **tri-state**, set by `join_valid` from each
row's target verdict:

| `valid` | Meaning | How derived |
|---------|---------|-------------|
| `True`  | The repo's URL is real/reachable. | The row's target resolved to a `True` verdict (AND across targets — currently one target per row, but the logic generalises to a future row carrying both a GitHub repo and a distinct non-GitHub URL). |
| `False` | The repo's URL is invalid / unreachable. | The row's target resolved to a non-`True` verdict (cache `valid=False`, or a `False` override pin). |
| *(empty)* | Orphan row — there is nothing to validate. | `_row_target` returned `None`: the row is neither a github `repo` nor has a `git_url`. |

So `validation.csv` is the per-target ledger and `value.csv`'s `valid` is its
per-row projection. Every non-orphan `value.csv` row's `valid` traces directly
to exactly one `validation.csv` row (matched by the row's target + type).

## Columns

| Column | Description |
|--------|-------------|
| `target` | The validated identity: a lowercase GitHub `owner/repo` slug, or a non-GitHub clone URL. |
| `type` | `github_repo` (validated via the GitHub Repos API) or `git_url` (validated via `git ls-remote`). |
| `sources` | Comma-separated, sorted list of ecosystems (`npm`, `pypi`, `crates`, `cpp`) whose packages resolve to this target. |
| `checked_at` | When the verdict was produced: the cache's `fetched_at` (GitHub) / `checked_at` (git), or the literal `override` when pinned via `overrides.csv`. |
| `valid` | `True`/`False` — whether the target was found real/reachable. |

## Refreshing

`build_validation` reads only existing caches, so refresh the verification first,
then rebuild:

```bash
# 1. Refresh the two validation caches (GitHub Repos API + git ls-remote).
#    This is the only step that does network I/O.
uv run python -m src.value.verify_git_urls

# 2. Roll the caches up into validation.csv and join the valid column.
uv run python -m src.value.build_validation
```

Or run the whole value pipeline, which wires both steps in order
(`verify` → `validation`):

```bash
uv run python -m src.value.run_value_pipeline
```

> **Prerequisite:** `build_validation` will not invent verdicts. If a target in
> `value.csv` has no entry in either cache, the **hard gate** aborts the build
> with the message *"Run `uv run python -m src.value.verify_git_urls` first."* —
> run that step (or the full pipeline) to populate the caches, then rebuild.
