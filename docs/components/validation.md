# Validation Table

`data/value/validation.csv` is the **git/GitHub validation audit table**. It
rolls up the two validation caches and decides, per validation *target*,
whether a repo's resolved URL is real and reachable. It is the audit trail
behind the `git_valid` column of [`data/value/value.csv`](../value.md): it
records which cache verdict applied, when the check ran, and which ecosystems
point at the target. `build_validation` joins that verdict back onto every
value row.

**Grain:** one row per *target*, not per repo. A target is a GitHub
`owner/repo` slug (type `github_repo`) or a non-GitHub clone URL (type
`git_url`). Value rows that share a target union their ecosystems into its
`sources` column.

## How it's built

Built by [`src/value/build_validation.py`](../../src/value/build_validation.py),
which runs as the `validation` step of the value pipeline runner — after
`unify`, and before `openssf-crit`, `eco-crit` and `criticality`.

It does one piece of network I/O: before rolling up, it refreshes the
non-GitHub `git ls-remote` reachability cache (`data/sources/git/urls.csv`,
TTL 365 days). The TTL makes a warm re-run a no-op; `--refresh` forces
a re-check regardless of age. It never touches the GitHub cache
(`data/sources/github/repos.csv`) — that is the GitHub Repos API record
maintained by `src.sources.github.fetch_repo_owner_data` and refreshed by the
earlier `resolve` step.

Steps:

1. **Collect targets** from `data/value/value.csv`. For each value row,
   `_row_target` picks a single target: the row's `repo` slug (type
   `github_repo`) when its `platform == github`, else its canonicalized
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
   target's validity via its `valid` column (`True`/`False`). The pin resolves
   to the override's `repo` target, else its `git_url` target, and **overrides
   whatever the cache said** (its `checked_at` becomes the literal string
   `override`). Two kinds of pin resolve to no target and are dropped:
   a `canonical_url`-only row is skipped **silently** — it declares "this
   project has no git upstream", so having no git target is correct — while a
   row with no `repo`, `git_url` or `canonical_url` at all is a
   misconfiguration and prints a warning.
4. **Hard gate:** every collected target must have a verdict. If any target has
   none, the step raises `SystemExit` listing the offenders and refusing to
   write — a missing verdict is treated as a pipeline error, never silently
   invalid (see *Refreshing* below).
5. **Write** `validation.csv` (sorted by `type`, then `target`) and **join** the
   per-target verdict back into the `git_valid` column of `value.csv`.

## The `valid` / `git_valid` columns

The same verdicts produce two columns. In `validation.csv`, `valid` is the
cache verdict (or override pin) for one target. In `value.csv`, `git_valid` is
the per-row projection of that verdict, set by `join_valid`. `git_valid` is
**host-agnostic**: a reachable non-GitHub upstream (sourceware / savannah / a
GitLab host / …) is valid on its own, not only a GitHub repo.

| `git_valid` | Meaning | How derived |
|---------|---------|-------------|
| `True`  | The repo's upstream is real/reachable. | The row's target verdict is `True` (GitHub Repos API for `platform == github` rows, `git ls-remote` for non-GitHub `git_url` rows) — **or** the row carries a GitLab `gl/` `repo_id`, which is itself a validity proof: the resolver only assigns it after the GitLab project API confirmed the project exists, more authoritative than `git ls-remote` (which can fail on hosts like salsa.debian.org even for live projects). This keeps the `repo_id ⇒ git_valid` invariant. |
| `False` | No reachable upstream. | Orphan rows (no target at all — nothing to validate), a target whose reachability check failed, or a github `repo` that 404s. |

Every non-orphan value row's `git_valid` traces to exactly one
`validation.csv` row, matched by target + type. `gl/`-id rows are the
exception: they are `True` whatever their `git_url` target's ls-remote verdict
says.

A valid non-GitHub upstream does **not** enter the risk / eligibility scope.
`load_top_repos` still filters to the platforms in `settings.json →
top_repos`; `git_valid` only records that the URL resolves.

## Columns

| Column | Description |
|--------|-------------|
| `target` | The validated identity: a lowercase GitHub `owner/repo` slug, or a non-GitHub clone URL. |
| `type` | `github_repo` (validated via the GitHub Repos API) or `git_url` (validated via `git ls-remote`). |
| `sources` | Comma-separated, sorted list of ecosystems (`npm`, `pypi`, `crates`, `cpp`) whose packages resolve to this target. |
| `checked_at` | When the verdict was produced: the cache's `fetched_at` (GitHub) / `checked_at` (git), or the literal `override` when pinned via `overrides.csv`. |
| `valid` | `True`/`False` — whether the target was found real/reachable. |

## Refreshing

`scripts/run-pipeline.sh` is the only supported entry point. Run the
`validation` step alone to rebuild `validation.csv` and re-join `git_valid`
onto `value.csv`. The step refreshes stale `ls-remote` entries first — its only
network I/O.

```bash
scripts/run-pipeline.sh --stage value --only validation
scripts/run-pipeline.sh --stage value --only validation --refresh  # re-check URLs
```

The `resolve` step refreshes the GitHub cache
(`data/sources/github/repos.csv`), so to refresh everything run the rollup (or
the whole value pipeline). It wires the steps in order — `eco-fetch` →
`canonical` → `resolve` → `unify` → `validation` → `openssf-crit` →
`eco-crit` → `criticality`:

```bash
uv run python -m src.value.run_value_pipeline --rollup
```

> **Prerequisite:** `build_validation` will not invent verdicts. If a target in
> `value.csv` has no entry in either cache, the **hard gate** aborts the build
> with the message *"Run `uv run python -m src.value.run_value_pipeline
> --rollup` first."* — run that (or the full pipeline) to populate the caches,
> then rebuild.
