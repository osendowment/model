# Concentration (risk component)

How dependent is a project on a handful of people? The concentration component
measures the **distribution of authorship** across a repo's contributors and
distills it into one **concentration-risk score (`score`, 0–100, higher = more
concentrated = more at-risk)** that feeds `data/risk/risk.csv` as the column
`concentration`. Two independent methods — a git-clone commit log and the GitHub
`/contributors` API — each produce a bus factor, an HHI, and contributor counts;
only the git `_5y` axis drives the score.

Scope: the 897 class-A value-class repos in the risk pipeline (see
[value.md](../value.md)). Build step: `src/risk/build_concentration.py`.

## Metrics Roadmap

Each leaf is one column with its source and the period it represents. `_full` =
all commits through the last complete year (`max(settings.years)` = 2025);
`_5y` = the last `concentration.window_years` (5) complete years, 2021–2025;
`_gh_alltime` = the GitHub API's uncapped lifetime count as of fetch (no
per-year breakdown, list capped near 500). Raw long-format signals are fetched
per-source under `data/sources/`; all derived columns are computed by
`build_concentration.py`. Only the git `_5y` bus-factor + HHI percentiles feed
`score`.

```
Concentration  → data/risk/concentration.csv  (897 A/B risk repos)
│
├── git-clone method  (data/sources/git/contributor-commits.csv)
│   ├── _full  (all commits through 2025)
│   │   ├── total_commits_git_full   ← Σ non-merge commits ≤ last complete year   [→2025]
│   │   ├── contributors_git_full    ← derived (merged non-bot identities)        [→2025]
│   │   ├── bf_commits_git_full      ← derived (bus factor)                       [→2025]
│   │   ├── bf_commits_git_full_p    ← derived (risk percentile, low BF → high)   [→2025]
│   │   ├── hhi_commits_git_full     ← derived (HHI, 0–10000)                     [→2025]
│   │   └── hhi_commits_git_full_p   ← derived (risk percentile, high HHI → high) [→2025]
│   └── _5y  (2021–2025 window — the scoring axis)
│       ├── commits_git_5y               ← Σ non-merge commits in window          [2021–2025]
│       ├── active_contributors_git_5y   ← derived (merged non-bot, active in win) [2021–2025]
│       ├── bf_commits_git_5y            ← derived (bus factor)                    [2021–2025]
│       ├── bf_commits_git_5y_p          ← derived (risk percentile)  ★ scores     [2021–2025]
│       ├── hhi_commits_git_5y           ← derived (HHI, 0–10000)                  [2021–2025]
│       └── hhi_commits_git_5y_p         ← derived (risk percentile)  ★ scores     [2021–2025]
│
├── GitHub /contributors method  (data/sources/github/contributor-commits.csv)
│   └── _gh_alltime  (lifetime, list capped ~500 — cross-check only)
│       ├── total_commits_gh_alltime        ← Σ /contributors `contributions`     [lifetime]
│       ├── total_contributors_gh_alltime   ← all /contributors rows (incl. bots) [lifetime]
│       ├── active_contributors_gh_alltime  ← derived (non-bot rows)              [lifetime]
│       ├── bf_commits_gh_alltime           ← derived (bus factor)                [lifetime]
│       ├── bf_commits_gh_alltime_p         ← derived (risk percentile)           [lifetime]
│       ├── hhi_commits_gh_alltime          ← derived (HHI, 0–10000)              [lifetime]
│       └── hhi_commits_gh_alltime_p        ← derived (risk percentile)           [lifetime]
│
└── score  (the component score)  ← geometric mean of bf_commits_git_5y_p
    │                                and hhi_commits_git_5y_p (0–100)
    └─ carried into risk.csv as the column `concentration` (this score only)
```

## How It Works

1. **Collect** — two fetchers dump raw long-format per-contributor data into
   `data/sources/`: the git fetcher walks `git log` on a bare clone, the GitHub
   fetcher hits the `/contributors` API. Each writes a `.status.csv` sidecar
   carrying `fetched_at` per repo, so a missing metric is distinguishable from a
   failed fetch.
2. **Join** — `build_concentration.py` joins both sources onto the 897 risk
   repos by `repo` slug (and reads `data/value/value.csv` for the A/B scope).
3. **Derive** — for each method: merge contributor identities, drop bots, then
   compute bus factor, HHI (0–10000), and contributor counts. The git method
   yields both a lifetime (`_full`) and a windowed (`_5y`) figure; the GitHub
   method yields lifetime only (`_gh_alltime`).
4. **Score** — `add_percentiles` turns the `_5y` bus factor and HHI into risk
   percentiles (`bf_commits_git_5y_p`, `hhi_commits_git_5y_p`); `score` is their
   geometric mean, an integer 0–100 (higher = more concentrated = more risk).
5. **Aggregate** — `aggregate_risk.py` carries **only** `score` into `risk.csv`,
   renamed to the column `concentration`.

Pipeline order. The GitHub `/contributors` fetcher runs inside the risk
pipeline; the git long-format fetcher is run separately (it clones repos, so it
is decoupled from the API-only pipeline run):

```
src.sources.git.contributors  (standalone clone-based dump)
                              ↘
src.risk.run_risk_pipeline:  … → contributors (GitHub) → … → concentration (build) → … → aggregate
```

## Collection

Both methods read a long-format raw file plus its status sidecar. Each row of
the raw file is one `(repo, contributor[, year])` tuple; the builder aggregates
over them. Join key into the risk-repo set is `repo` for both.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `git/contributor-commits.csv` | `src/sources/git/contributors.py` | long raw: `repo, author_name, author_email, year, commits` from `git log --no-merges` on a bare treeless clone (mailmap-resolved `%aN`/`%aE`) | `repo` |
| `git/contributor-commits.status.csv` | `src/sources/git/contributors.py` | per-repo git-fetch status + `fetched_at` | `repo` |
| `github/contributor-commits.csv` | `src/sources/github/fetch_contributors_metrics.py` | long raw: `repo, login, contributions, account_type` from the `/repos/{repo}/contributors` endpoint | `repo` |
| `github/contributor-commits.status.csv` | `src/sources/github/fetch_contributors_metrics.py` | per-repo GitHub-fetch status + `fetched_at` | `repo` |
| `value/value.csv` | value pipeline | A/B scope (`load_risk_repos`) | `repo` |

### Two methods, two different lenses

| | git-clone log | GitHub `/contributors` |
|---|---|---|
| Sees | every committer, with author dates | accounts only, lifetime cumulative |
| Periods | `_full` **and** `_5y` (windowed) | `_gh_alltime` (lifetime only) |
| Identity | raw name+email pairs → union-find merge | already keyed by GitHub login |
| Limit | times out on kernel-scale mirrors | list capped near 500 → under-counts big repos |
| Role | **drives `score`** (the `_5y` axis) | parallel cross-check, never scored |

The git method is authoritative because it carries author dates (so it can
honour a 2021–2025 window) and sees every contributor. The GitHub method has no
per-year breakdown and truncates the contributor list near 500 entries, so its
columns are deliberately labelled `_gh_alltime` — an uncapped lifetime figure as
of `github_fetched_at` (may include the partial current year), never `_full`/`_5y`.

## Processing & scoring

### Identity merge + bot drop

The git method's raw rows are keyed by mailmap-resolved `(author_name,
author_email)` pairs. The builder additionally union-finds identities that share
a normalised email **or** a full name (`merge_identity_groups`), so a person who
committed under several addresses counts once. Bot identities are then dropped
(`_is_bot_identity` for git, `account_type == "Bot"` / `is_bot(login)` for
GitHub) before any metric is computed. The GitHub method needs no merging —
`/contributors` is already keyed by account.

### Bus factor and HHI

Over the merged, bot-free per-contributor commit counts:

| Metric | Definition |
|---|---|
| **bus factor** (`bf_commits_*`) | fewest contributors whose combined commits reach `bus_factor_threshold` (0.5 = the people covering 50% of commits). Low = concentrated. |
| **HHI** (`hhi_commits_*`) | Herfindahl–Hirschman index of commit shares, scaled to 0–10000. High = concentrated. |

Both are **undefined** (blank, not `0`) for a repo with no positive-commit
contributor — a real `0` would falsely rank as both maximum-concentration bus
factor and minimum-concentration HHI, so a blank keeps such repos out of both
percentile rankings.

### The percentiles (`_p`)

`add_percentiles` ranks each metric into a 0–100 **risk percentile**, with the
direction chosen per metric so that *more concentrated always ranks higher*:

| Column | Basis | `higher_is_worse` | Direction |
|---|---|---|---|
| `bf_commits_git_5y_p` | `bf_commits_git_5y` | `False` | low bus factor → high percentile |
| `hhi_commits_git_5y_p` | `hhi_commits_git_5y` | `True` | high HHI → high percentile |
| `bf_commits_git_full_p` | `bf_commits_git_full` | `False` | (not scored) |
| `hhi_commits_git_full_p` | `hhi_commits_git_full` | `True` | (not scored) |
| `bf_commits_gh_alltime_p` | `bf_commits_gh_alltime` | `False` | (not scored) |
| `hhi_commits_gh_alltime_p` | `hhi_commits_gh_alltime` | `True` | (not scored) |
| **`score`** | geom mean of the two `_5y` `_p` | — | the concentration-risk score |

Only the two `_5y` percentiles compose the score (`composite_cols =
["bf_commits_git_5y_p", "hhi_commits_git_5y_p"]`). The `_full` and `_gh_alltime`
percentiles are emitted for inspection but do **not** feed `score`. The
**geometric mean** means a repo only scores as low-risk when *both* axes agree
it is well-distributed — one concentrated axis pulls the product up.

## Output

### `data/risk/concentration.csv` (per-dimension build)

24 columns, one row per risk repo. No `fetched_at` *value* columns — per-method
timestamps live in `github_fetched_at` / `git_fetched_at`.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `total_commits_gh_alltime` | Σ of `/contributors` contributions (incl. bots) |
| `total_contributors_gh_alltime` | all `/contributors` rows (incl. bots) |
| `active_contributors_gh_alltime` | non-bot `/contributors` rows |
| `bf_commits_gh_alltime` | bus factor — GitHub method, lifetime |
| `bf_commits_gh_alltime_p` | risk percentile of `bf_commits_gh_alltime` |
| `hhi_commits_gh_alltime` | HHI (0–10000) — GitHub method, lifetime |
| `hhi_commits_gh_alltime_p` | risk percentile of `hhi_commits_gh_alltime` |
| `total_commits_git_full` | Σ non-merge commits through 2025 |
| `contributors_git_full` | merged non-bot identities, `_full` |
| `bf_commits_git_full` | bus factor — git method, `_full` |
| `bf_commits_git_full_p` | risk percentile of `bf_commits_git_full` |
| `hhi_commits_git_full` | HHI (0–10000) — git method, `_full` |
| `hhi_commits_git_full_p` | risk percentile of `hhi_commits_git_full` |
| `commits_git_5y` | Σ non-merge commits in 2021–2025 |
| `active_contributors_git_5y` | merged non-bot identities active in window |
| `bf_commits_git_5y` | bus factor — git method, `_5y` |
| `bf_commits_git_5y_p` | risk percentile of `bf_commits_git_5y` **(scores)** |
| `hhi_commits_git_5y` | HHI (0–10000) — git method, `_5y` |
| `hhi_commits_git_5y_p` | risk percentile of `hhi_commits_git_5y` **(scores)** |
| `score` | **concentration-risk score** (geom-mean of the two `_5y` `_p`, 0–100) |
| `github_fetched_at` | when the GitHub `/contributors` data was fetched |
| `git_fetched_at` | when the git-clone log was fetched |

### `data/risk/risk.csv` (aggregate)

`aggregate_risk.py` carries **only this component's `score`** into `risk.csv`,
renamed to the column `concentration`. Every other column above stays in the
per-dimension CSV. `risk.csv` today is just `repo, repo_id, concentration,
complexity, security, funding, workload, score` — one score per component plus
the overall geometric-mean `score`.

## Coverage

Of the 897 A/B risk repos (counts refresh on re-run):

| Signal | Repos | % |
|---|---:|---:|
| git `_5y` metrics (`bf`/`hhi` populated) | 830 | 92.5% |
| **`score`** (concentration-risk score) | 830 | 92.5% |
| git `_full` metrics | 894 | 99.7% |
| `git_fetched_at` present | 895 | 99.8% |
| GitHub `_gh_alltime` metrics | 891 | 99.3% |
| `github_fetched_at` present | 892 | 99.4% |

`score` distribution: min **1** · p25 **27** · p50 **71** · p75 **87** · max
**100**. The high median reflects how concentrated this cohort is — **71.6%** of
scored repos have a git `_5y` bus factor of 1 (a single contributor covers half
the window's commits). The ~7.5% of repos with no `score` are those whose git
`_5y` axis is blank — repos with no commits in 2021–2025 (e.g. archived /
frozen projects), or where the git clone failed/timed out (kernel-scale mirrors).

## Limitations

- **`_5y` window or no score.** A repo with zero commits in 2021–2025 — archived,
  frozen, or whose clone timed out — has no `bf_commits_git_5y`, so no `score`.
  This is intentional: a stale repo cannot be scored on *current* concentration,
  and a blank keeps it out of the percentile ranking rather than faking a value.
- **Commits ≠ effort.** Both methods count authored commits, not lines, reviews,
  triage, or maintenance burden. A reviewer or release manager who rarely commits
  is invisible, so a repo can read as more concentrated than it truly is.
- **GitHub method under-counts big repos.** `/contributors` caps the list near
  500 entries and exposes no per-year breakdown, so its `_gh_alltime` columns
  are kept only as a cross-check and never feed `score`.
- **git method can time out.** A bare treeless clone of kernel-scale mirrors
  (e.g. `archlinux/linux`) can exceed the fetch budget; the status sidecar
  records the failure and the repo's git columns stay blank.
- **Identity merge is heuristic.** Union-find over shared email/name catches most
  aliases, but a contributor who never reused an email or canonical name across
  identities will still be split — inflating contributor count and deflating
  concentration slightly.
- **`score` is a percentile, not a class.** It is a 0–100 rank within this
  cohort, not an absolute rating, and it is one of five inputs to the overall
  `risk.csv` `score` (geometric mean of the component scores).
