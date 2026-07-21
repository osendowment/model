# Git (clone-based analysis)

Code metrics computed by cloning each repo and running local tools — no
hosting-platform API in the measurement itself. Every fetcher clones the
repo's real `git_url` (from `value.csv`), so GitLab repos are analyzed exactly
like GitHub ones; this is why most risk signals are host-agnostic. The two
exceptions are the SHA-anchoring helpers (`commits_years`, `resolve_head`),
which query the GitHub Commits API to pick *which* commit to analyze.

`data/sources/git/` is also the shared store for **all** per-(repo, sha)
long-format metric files — including ones written by other source modules
(OpenSSF Scorecard, deps.dev) via `src.sources.git.long_format`.

## Clone strategies (`src/sources/git/clone.py`)

All kill the whole process group on timeout and accept a `repo_url` override
(GitLab-capable):

| Helper | Contents | Used for |
|--------|----------|----------|
| `download_tarball` | codeload tar.gz, no `.git` (GitHub-only) | source-only analysis, small repos |
| `sparse_clone` | depth-1, `--filter=blob:none`, checkout limited to `SOURCE_EXTS` code globs | scc / lizard snapshots |
| `bare_blobless_clone` | full commit graph, lazy blobs; `ref` pinned as `_pinned` branch | history walking needing trees |
| `bare_treeless_clone` | `--filter=tree:0` — commits only, lightest | author/date walking (`contributors.py`) |
| `fetch_all_blobs` | bulk refetch into a blobless clone | blame-scale work |

`sparse_clone`'s timeout scales with repo size (60s + 60s per 100 MB), and a
failed git step **raises** — it must never be recorded as a genuine 0-LOC repo.

## SHA anchoring

All sha-pinned analyses (scc, lizard) key off `commits-years.csv`:

| Step | Module | behavior |
|------|--------|-----------|
| Per-year SHAs | `commits_years.py` | GitHub Commits API, per (repo, year): newest + oldest commit in the year on the default branch (2 calls: `per_page=1`, then `&page={count}` from the Link header) → `first_sha`, `last_sha`, `commits`. Inactive years stored with empty SHAs, `commits=0` |
| Snapshot pick | `resolve_snapshot_sha` | target year's `last_sha`, cascading back through earlier years (`SNAPSHOT_WALKBACK_YEARS = 30`); no usable sha → no row |
| Dormant repos | `resolve_head.py` | latest default-branch commit capped at end of the last complete year, stored under its **real** year |
| Off-mainline correction | `resolve_mainline_sha` / `corrected_clone_sha` | the API can return a merged side-branch commit (e.g. a shared CI-template commit); fetchers verify the pinned SHA is on the default branch's first-parent line and, if not, *check out* the mainline commit at the year-end cutoff — while still **recording** metrics under the pinned SHA (the builders' join key) |

## The long format (`long_format.py`)

All sha-pinned metric files share one schema:

    repo, repo_id, git_url, commit_sha, metric, value, checked_at

Key = `(repo, commit_sha, metric)`; upserts replace by key, so snapshots at
older SHAs survive as a time-series. Writers hold an `fcntl` lock on a `.lock`
sidecar and write atomically (temp file + rename), so concurrent fetchers
can't lose each other's rows. A blank `git_url` is resolved host-agnostically
from `value.csv`. `latest_sha_per_repo` + `project_to_wide` are the read side
the risk builders use.

## Raw Data (`data/sources/git/`)

| File | Writer | Contents |
|------|--------|----------|
| `commits-years.csv` | `commits_years.py` + `resolve_head.py` | `repo, repo_id, git_url, year, first_sha, last_sha, commits, fetched_at` |
| `scc.csv` | `fetch_sha_metrics.py` / `fetch_scc.py` | long: `files, loc, sloc, uloc, complexity, complexity_density`, summed over a source-language allow-list |
| `lizard.csv` | `fetch_sha_metrics.py` | long: `files`, `cyclomatic_{total,avg,max}`, `cognitive_{total,avg,max}` (the file also carries `halstead_*` / `maintainability_index` rows the fetcher does not emit) |
| `contributor-commits.csv` | `contributors.py` | long raw: `repo, repo_id, git_url, author_name, author_email, year, commits` — one row per mailmap-resolved author-year, merges excluded; identity merging happens in `build_concentration` |
| `contributor-commits.status.csv` | `contributors.py` | per-repo sidecar: `status` ∈ {ok, no_commits, clone_failed, timeout, error}, `distinct_authors, commits_total, clone_seconds, error, fetched_at` |
| `churn.csv` | `src/sources/github/fetch_churn.py` | wide, one row per repo: `repo, repo_id, git_url, analyzed_through_year, commits_5y_examined, churn_5y_{added,deleted,total}, churn_files_count, top_file_path, top_file_churn, elapsed_s, fetched_at` — source-file lines added/deleted over 2021–2025 |
| `openssf.csv` | `src/sources/openssf/scorecard.py` | long: Scorecard `score` + per-check scores, pinned to the commit Scorecard scanned |
| `depsdev.csv` | `src/sources/depsdev/fetch.py` | long: deps.dev-mirrored Scorecard rows (fallback when no local scan) |
| `urls.csv` | `src/value/git_urls.py` (value stage) | non-GitHub clone-URL validity cache keyed by **URL**: `url, valid, method, checked_at` (`git ls-remote`) |

Consumers: `src/risk/build_complexity.py` (scc, lizard),
`build_concentration.py` (contributor-commits), `build_security.py` (openssf,
depsdev), `build_workload.py` (commits-years) — see
[complexity](../components/complexity.md), [concentration](../components/concentration.md),
[security](../components/security.md), [workload](../components/workload.md).
Coverage/funnel counts: the preview pipeline sheet → Risk.

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/git/commits_years.py` | Per (repo, year) first/last SHA + commit count (GitHub API) |
| `src/sources/git/resolve_head.py` | Snapshot SHA for dormant repos (dated rows only) |
| `src/sources/git/fetch_sha_metrics.py` | One sparse checkout → scc + both lizard passes → `scc.csv` + `lizard.csv` |
| `src/sources/git/fetch_scc.py` | scc-only fetcher (helpers reused by `fetch_sha_metrics`) |
| `src/sources/git/contributors.py` | Treeless clone + `git log` → contributor-commits long + status sidecar |
| `src/sources/github/fetch_churn.py` | Bare clone + `git log --numstat --no-merges` over 2021–2025 → `churn.csv` (clone-based; filed under the github source folder) |
| `src/sources/git/clone.py` / `long_format.py` / `disk.py` | Shared clone / long-CSV / disk-safety helpers |

```bash
uv run python -m src.sources.git.commits_years --limit 10
uv run python -m src.sources.git.resolve_head
uv run python -m src.sources.git.fetch_sha_metrics --limit 5
uv run python -m src.sources.git.contributors --limit 20
uv run python -m src.sources.git.contributors --inspect curl/curl
```

## Auditability & caveats

| Concern | behavior |
|---------|-----------|
| Fetch dates | every file carries `fetched_at` / `checked_at` |
| Failure vs zero | `contributor-commits.status.csv` records `timeout` / `clone_failed` explicitly; a failed sparse clone raises instead of writing a fake 0-LOC snapshot |
| Re-run skip | scc skips a (repo, sha) only when all 6 metrics are present **and** `loc > 0`; lizard rows are sha-pinned with a 365-day TTL gating only same-sha re-runs; `--force` bypasses both |
| Timeouts | 300s default per clone + `git log` (`contributors.py --timeout` for kernel-scale mirrors); scc 120s; lizard 900s scaled up per GB of repo |
| Pathological files | files > 2 MB and Fortran sources skipped in lizard (OOM-prone / generated code) |
| Disk safety (`disk.py`) | startup free-space banner, stale `ose-fetch-*` temp-dir sweep, `--max-disk-gb` poll that stops scheduling clones and flushes partial results when temp space runs low |
