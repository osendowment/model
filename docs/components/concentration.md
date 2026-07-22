# Concentration (risk component)

How dependent is a project on a handful of people? The concentration component
measures the **distribution of authorship** across a repo's contributors. It
emits one **concentration-risk score** (`score`, 0–100, higher = more
concentrated = more at-risk), which feeds `data/risk/risk.csv` as the column
`concentration`.

One source produces every column: the **git-clone commit log** (`git log
--no-merges` on a bare treeless clone, mailmap applied, identities merged, bots
dropped). It yields a bus factor, an HHI, and contributor counts over both a
lifetime and a 5-year window. Only the `_5y` axis drives the score. No host
contributors API is involved, so no axis goes blank on a GitLab repo.

Scope: the valid class-A top repos in the risk pipeline — GitHub + GitLab,
archived included (see [value.md](../value.md)). GitHub and GitLab repos go
through the identical code path. Build step:
`src/risk/build_concentration.py`.

## Scored components: Bus Factor + HHI

`concentration.csv` carries 17 columns. The **`score` uses exactly two**, both
over the `_5y` window (2021–2025). Each is read on an **absolute** 0–100 scale
(`100 / bf` and `hhi / 100`), then combined as a geometric mean:

- **Bus factor** (`bf_commits_git_5y`) — the fewest merged, non-bot contributors
  whose cumulative commit share reaches 50% (`bus_factor_threshold`). A **low**
  bus factor means high risk. A bus factor of 1 means one person authored half
  the window's commits.
- **HHI** (`hhi_commits_git_5y`) — the Herfindahl–Hirschman index of commit
  shares, `10000 · Σ pᵢ²`. It ranges from `10000 / n` (commits spread evenly) up
  to `10000` (one author writes everything). A **high** HHI means high risk.
- **No active human in the window** — a repo with **no human commit in
  2021–2025** is imputed the worst valid pair, **bus factor `1` and HHI
  `10000`**. A project with no active human maintainer in five years is a
  maximal single point of failure, so it ranks as fully concentrated rather than
  dropping out. Three cases hit this rule: `no commits in 5y` (dormant or
  archived), `no human commits in 5y` (bot-only window), and `no commits through
  last complete year` (new repo). The imputation applies **only to
  successfully-fetched repos**. A clone or log failure writes no data and is the
  *only* unscored case, so a fetch failure never reads as a measurement. The
  score is therefore **fully populated** across the risk set.

```
score = max(1, √( (100 / bf_commits_git_5y) × (hhi_commits_git_5y / 100) ))
      = max(1, √( hhi_commits_git_5y / bf_commits_git_5y ))      → 2 decimals
```

Both factors are absolute scales, **not within-table percentiles**. `100 / bf`
maps bus factor 1 → 100, 2 → 50, 4 → 25. `hhi / 100` maps the one-author
monoculture (HHI 10000) → 100. The risk set is dominated by repos with bus
factor 1, so a percentile basis would collapse most of the table into one tie
block. A 99%-single-author repo (HHI 9868) would then rank far below its true
concentration, because so much of the population sits even higher. The absolute
score reads both inputs at face value, and a repo's score does not move when the
surrounding population changes.

Every other column — the `_full` lifetime figures, the contributor/commit
counts, and the `*_p` percentiles — is emitted for inspection only. **None of
them feed `score`.**

## Metrics Roadmap

Each leaf is one column with its source and the period it represents. `_full` =
all commits through the last complete year (`max(settings.years)` = 2025). `_5y`
= the last `concentration.window_years` (5) complete years, 2021–2025. The raw
long-format signal is fetched under `data/sources/git/`. `build_concentration.py`
computes every derived column. Only the `_5y` bus factor and HHI feed `score`.

```
Concentration  → data/risk/concentration.csv  (one row per risk-scope repo)
│
├── git-clone commit log  (data/sources/git/contributor-commits.csv)
│   ├── _full  (all commits through 2025)
│   │   ├── total_commits_git_full   ← Σ non-merge commits ≤ last complete year   [→2025]
│   │   ├── contributors_git_full    ← derived (merged non-bot identities)        [→2025]
│   │   ├── bf_commits_git_full      ← derived (bus factor)                       [→2025]
│   │   ├── bf_commits_git_full_p    ← derived (risk percentile, low BF → high)   [→2025]
│   │   ├── hhi_commits_git_full     ← derived (HHI, 0–10000)                     [→2025]
│   │   └── hhi_commits_git_full_p   ← derived (risk percentile, high HHI → high) [→2025]
│   └── _5y  (2021–2025 window — the scoring axis)
│       ├── commits_git_5y               ← Σ non-merge commits in window           [2021–2025]
│       ├── active_contributors_git_5y   ← derived (merged non-bot, active in win) [2021–2025]
│       ├── bf_commits_git_5y            ← derived (bus factor)       ★ scores     [2021–2025]
│       ├── bf_commits_git_5y_p          ← derived (risk percentile, audit only)   [2021–2025]
│       ├── hhi_commits_git_5y           ← derived (HHI, 0–10000)     ★ scores     [2021–2025]
│       └── hhi_commits_git_5y_p         ← derived (risk percentile, audit only)   [2021–2025]
│
└── score  (the component score)  ← geometric mean of 100/bf_commits_git_5y
    │                                and hhi_commits_git_5y/100 (0–100, absolute)
    └─ carried into risk.csv as the column `concentration` (this score only)
```

## How It Works

1. **Collect** — `src/sources/git/contributors.py` walks `git log` on a bare
   treeless clone and writes long-format per-contributor rows into
   `data/sources/git/`. It also writes a `.status.csv` sidecar with `fetched_at`
   per repo, so a missing metric is distinguishable from a failed fetch.
2. **Join** — `build_concentration.py` joins the source onto the risk repos by
   the stable `repo_id`, so a renamed or moved repo keeps the data collected
   under its old name. It reads `data/value/value.csv` via `load_top_repos` for
   the valid class-A scope.
3. **Derive** — merge contributor identities, drop bots, then compute bus factor,
   HHI (0–10000), and contributor counts. The log carries author dates, so it
   yields both a lifetime (`_full`) and a windowed (`_5y`) figure.
4. **Score** — `score = max(1, √(hhi/bf))` over the `_5y` bus factor and HHI: the
   geometric mean of the absolute scales `100/bf` and `hhi/100`, as a 2-decimal
   0–100 value (higher = more concentrated = more risk). `add_percentiles` emits
   the four `*_p` columns as audit references.
5. **Aggregate** — `aggregate_risk.py` carries **only** `score` into `risk.csv`
   as the `concentration` column.

Pipeline order (`src/risk/run_risk_pipeline.py`, run via `scripts/run-pipeline.sh
--from-stage risk`). The `git-contributors` fetcher runs **inside** the risk
pipeline. It is the only source of the concentration score and of workload's
per-contributor divisor, so a repo newly entering scope is measured on the same
run that scores it:

```
… → git-contributors (clone) → … → concentration (build) → … → aggregate
```

## Collection

The builder reads one long-format raw file plus its status sidecar. Each raw row
is one `(repo, contributor, year)` tuple. The join key into the risk-repo set is
the stable `repo_id`; rows with a blank `repo_id` are skipped.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `git/contributor-commits.csv` | `src/sources/git/contributors.py` | long raw: `repo, repo_id, git_url, author_name, author_email, year, commits` from `git log --no-merges` on a bare treeless clone (mailmap-resolved `%aN`/`%aE`) | `repo_id` |
| `git/contributor-commits.status.csv` | `src/sources/git/contributors.py` | per-repo git-fetch status + `fetched_at` | `repo_id` |
| `value/value.csv` | value pipeline | valid class-A top-repo scope (`load_top_repos`) | `repo_id` |

The commit log is authoritative for two reasons. It sees **every** contributor
(no API list cap), and it carries **author dates**, so it can honor a 2021–2025
window rather than only a lifetime total. It reads a clone, not a host API, so
GitHub and GitLab repos go through the identical code path.

## Processing & scoring

### Identity merge + bot drop

The raw rows are keyed by mailmap-resolved `(author_name, author_email)` pairs;
the repo's own `.mailmap` is applied at fetch time. The builder then union-finds
identities that share a normalized email **or** a full name
(`merge_identity_groups`), so a person who committed under several addresses
counts once. It drops bot identities (`_is_bot_identity`) before it computes any
metric.

### Bus factor and HHI

Over the merged, bot-free per-contributor commit counts:

| Metric | Definition |
|---|---|
| **bus factor** (`bf_commits_*`) | fewest contributors whose combined commits reach `bus_factor_threshold` (0.5 = the people covering 50% of commits). Low = concentrated. |
| **HHI** (`hhi_commits_*`) | Herfindahl–Hirschman index of commit shares, scaled to 0–10000. High = concentrated. |

A real `0` would read as both a maximum-concentration bus factor and a
minimum-concentration HHI, so `0` is never written. The two windows then
differ:

- `*_git_full` is left **blank** when the repo has no positive-commit
  contributor at all, keeping it out of the percentile rankings.
- `*_git_5y` is **imputed** to the maximum-concentration pair
  (`bf = 1`, `hhi = 10000`) when the window holds no human commits, because
  the scored dimension must not go blank for a dormant repo. Those repos score
  100 — the top of the scale — rather than dropping out. A clone or log failure
  is the only thing that leaves the window metrics blank.

### The percentiles (`_p`)

`add_percentiles` ranks each metric into a 0–100 **risk percentile**, with the
direction chosen per metric so that *more concentrated always ranks higher*:

| Column | Basis | `higher_is_worse` | Direction |
|---|---|---|---|
| `bf_commits_git_5y_p` | `bf_commits_git_5y` | `False` | low bus factor → high percentile |
| `hhi_commits_git_5y_p` | `hhi_commits_git_5y` | `True` | high HHI → high percentile |
| `bf_commits_git_full_p` | `bf_commits_git_full` | `False` | (audit only) |
| `hhi_commits_git_full_p` | `hhi_commits_git_full` | `True` | (audit only) |

**None of the four percentiles feed `score`.** The builder passes
`composite_cols = []`; the absolute formula `max(1, √(hhi/bf))` over the `_5y`
axis fills `score` instead (see *Scored components*). All four `_p` columns are
audit references only. The **geometric mean** inside the score means a repo
scores as low-risk only when *both* axes agree it is well-distributed — one
concentrated axis pulls the product up. The percentile CDFs rank the whole
top-repo population (GitHub + GitLab together).

## Output

### `data/risk/concentration.csv` (per-dimension build)

17 columns, one row per risk repo. No `fetched_at` *value* columns — the fetch
timestamp lives in `git_fetched_at`.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `total_commits_git_full` | Σ non-merge commits through 2025 |
| `contributors_git_full` | merged non-bot identities, `_full` |
| `bf_commits_git_full` | bus factor — `_full` |
| `bf_commits_git_full_p` | risk percentile of `bf_commits_git_full` |
| `hhi_commits_git_full` | HHI (0–10000) — `_full` |
| `hhi_commits_git_full_p` | risk percentile of `hhi_commits_git_full` |
| `commits_git_5y` | Σ non-merge commits in 2021–2025 |
| `active_contributors_git_5y` | merged non-bot identities active in window |
| `bf_commits_git_5y` | bus factor — `_5y` **(scores)** |
| `bf_commits_git_5y_p` | risk percentile of `bf_commits_git_5y` (audit only) |
| `hhi_commits_git_5y` | HHI (0–10000) — `_5y` **(scores)** |
| `hhi_commits_git_5y_p` | risk percentile of `hhi_commits_git_5y` (audit only) |
| `score` | **concentration-risk score** — `max(1, √(hhi_commits_git_5y / bf_commits_git_5y))`, the geom-mean of the absolute scales `100/bf` and `hhi/100` (0–100, 2 decimals) |
| `comment` | edge-case note on the `_5y` axis (auditability), else empty. All but the last are imputed `bf=1`/`HHI=10000`: `no commits in 5y` (dormant), `no human commits in 5y` (bot-only window), `no commits through last complete year` (only in-progress-year activity). `git fetch <status>` / `no git data` (fetch failed → **blank**, the only unscored case) |
| `git_fetched_at` | when the git-clone log was fetched |

### `data/risk/risk.csv` (aggregate)

`aggregate_risk.py` carries **only this component's `score`** into `risk.csv`
as the `concentration` column. Every other column above stays in the
per-dimension CSV. `risk.csv` is `repo, repo_id, concentration,
complexity, security, workload, risk_score` — one score per component plus
the overall geometric-mean `risk_score` (blank unless all four component
scores are present).

## Coverage

See the preview pipeline sheet → Risk → Concentration for current per-signal coverage over the top repos and the score distribution.

## Limitations

- **No human in the window is imputed; fetch failures are not.** A repo with no
  human commit in 2021–2025 (dormant, bot-only, or new) scores as maximally
  concentrated (`bf = 1`, `HHI = 10000`) — see *Scored components*. Only the
  genuinely *unmeasured* stays blank: a clone or `git log` that failed, which the
  status sidecar records. The distinction is auditable, because the imputation
  lives in `git_metrics`, which runs only after a successful fetch.
  `scripts/pipeline_health.py` asserts `bf_commits_git_5y`, `hhi_commits_git_5y`
  and `score` are fully populated, so any blank surfaces as a fetch gap to fix.
- **Commits ≠ effort.** The commit log counts authored commits, not lines,
  reviews, triage, or maintenance burden. A reviewer or release manager who
  rarely commits is invisible, so a repo can read as more concentrated than it
  is.
- **Identity merge is heuristic.** Union-find over shared email or name catches
  most aliases. A contributor who never reused an email or canonical name across
  identities is still split, which inflates the contributor count and slightly
  deflates concentration.
- **`score` is a continuous scale, not a class.** It is an absolute 0–100 scale,
  not a within-cohort percentile (see *Scored components*). It is one of four
  inputs to the overall `risk.csv` `risk_score`, the geometric mean of the
  component scores.
