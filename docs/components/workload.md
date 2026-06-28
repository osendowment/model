# Workload (risk component)

How much maintenance burden rests on each active contributor? The workload
component normalises three burdens — codebase size, security debt, and issue
backlog — **per active contributor (AC)**, then folds the three per-AC
percentiles into one **workload-risk score (`score`)** that feeds
`data/risk/risk.csv` as the `workload` column.

Scope: the top repos in the risk pipeline (see
[value.md](../value.md)). Build step: `src/risk/build_workload.py` — unusually,
it must run **after** `build_complexity`, `build_security`, and
`build_concentration`, because it reads their per-dimension CSVs to get the LOC,
CVE, and AC inputs.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[2021–2025]` = the settings `years` window; `[EOY]` = anchored to the end of
the last complete year (2025). Raw signals are fetched per-source under
`data/sources/`; the three burden inputs come from sibling risk CSVs; derived
columns are computed by `build_workload.py`.

```
Workload  → data/risk/workload.csv  (the top risk repos)
│
├── Activity / liveness
│   ├── repo_age_years          ← repos.csv created_at → EOY                       [EOY]
│   ├── push_cadence_years      ← commits-years.csv (count of years with ≥1 commit) [2021–2025]
│   ├── openssf_maintained      ← openssf/checks.csv "Maintained" sub-check (0-10)  [most recent]
│   ├── has_issues              ← repos.csv (GH /repos)                            [most recent]
│   └── pushed_at               ← repos.csv (GH /repos)                            [most recent]
│
├── Issue backlog
│   ├── issues_opened_5y        ← issues.csv (metric=opened_issues, summed)        [2021–2025]
│   ├── issues_closed_5y        ← issues.csv (metric=closed_issues, summed)        [2021–2025]
│   ├── net_new_issues_5y       ← derived (opened_5y − closed_5y)                  [2021–2025]
│   ├── issue_close_ratio       ← derived (closed_5y / opened_5y)                  [2021–2025]
│   ├── slope_opened, slope_closed ← derived (OLS slope of yearly counts)          [2021–2025]
│   ├── issue_trend_score       ← derived (vol-normalised slope_closed − slope_opened) [2021–2025]
│   ├── issue_close_ratio_p     ← derived (percentile, info-only)                  [2021–2025]
│   └── issue_trend_score_p     ← derived (percentile, info-only)                  [2021–2025]
│
├── Per-AC burden  (AC = active_contributors_git_5y, from concentration.csv)
│   ├── active_contributors_git_5y ← concentration.csv (the AC denominator)        [2021–2025]
│   ├── loc_per_ac              ← complexity.csv loc_eoy / AC                       [EOY]
│   ├── cve_per_ac             ← security.csv cve_count_5y / AC                     [2021–2025]
│   ├── nni_per_ac             ← net_new_issues_5y / AC                             [2021–2025]
│   ├── loc_per_ac_p, cve_per_ac_p, nni_per_ac_p ← derived (risk percentiles)      [2021–2025]
│
└── score  (the workload score) ← derived (geometric mean of loc_per_ac_p, cve_per_ac_p, nni_per_ac_p)  [2021–2025]
    └─ carried into risk.csv as the column `workload`
```

## How It Works

1. **Collect** — `build_workload.py` reads the raw GitHub/Git/OpenSSF source
   CSVs (`repos.csv`, `commits-years.csv`, `openssf/checks.csv`, `issues.csv`).
2. **Join other components** — it joins the three sibling risk CSVs by `repo` to
   pull `loc_eoy` (complexity), `cve_count_5y` (security), and
   `active_contributors_git_5y` (concentration, the AC denominator).
3. **Derive per-AC** — divide each burden by AC → `loc_per_ac`, `cve_per_ac`,
   `nni_per_ac`; also compute the issue-trend slopes and ratio.
4. **Score** — `score` = geometric mean of the three per-AC risk percentiles.
5. **Aggregate** — `aggregate_risk.py` carries **only** this component's `score`
   into `risk.csv` as the `workload` column.

Pipeline order (`src/risk/run_risk_pipeline.py`) — workload runs last because it
consumes the three earlier builds:

```
concentration → complexity → security → funding-build → workload
```

## Collection

Workload reads seven inputs: four raw source CSVs plus three sibling risk-component
CSVs. The three component CSVs are not external fetches — they are the upstream
build outputs this component depends on, joined by `repo`.

| Input file | Fetcher / producer | Collects | Join key |
|---|---|---|---|
| `data/value/value.csv` | value pipeline | A/B risk-repo scope | `repo` |
| `data/sources/github/repos.csv` | `src/sources/github/fetch_repos.py` (GH /repos) | `created_at`, `has_issues`, `pushed_at` | `repo` |
| `data/sources/github/git/commits-years.csv` | git-clone commit analysis | per `(repo, year)` commit counts → `push_cadence_years` | `repo` |
| `data/sources/openssf/checks.csv` | `src/sources/openssf/` (Scorecard) | "Maintained" sub-check (0–10) | `repo` |
| `data/sources/github/issues.csv` | `src/sources/github/fetch_issue_metrics.py` | long: `repo, repo_id, year, metric, value` (metric ∈ opened_issues, closed_issues) | `repo` |
| `data/risk/complexity.csv` | `src/risk/build_complexity.py` | `loc_eoy` (codebase size) | `repo` |
| `data/risk/security.csv` | `src/risk/build_security.py` | `cve_count_5y` (security debt) | `repo` |
| `data/risk/concentration.csv` | `src/risk/build_concentration.py` | `active_contributors_git_5y` (AC denominator) | `repo` |

### Issues are long-format

`issues.csv` is long (`repo, repo_id, year, metric, value`), one row per
`(repo, year, metric)`. The build pivots it to `{metric: {repo: {year: count}}}`
and backfills missing window years with `0`. A repo **present** in `issues.csv`
(even all-zero) was genuinely fetched — a real 0; a repo **absent** was never
fetched, so all its issue figures stay blank rather than 0, preventing a fetch
gap from masquerading as "zero issues" and skewing the per-AC percentiles.

## Processing & scoring

### Per-AC normalisation

`AC = active_contributors_git_5y` — distinct non-bot contributors who authored a
commit in 2021–2025 (git-clone method, from `concentration.csv`). Each burden is
divided by AC; when `AC = 0` or missing, all three per-AC values are blank.

| Column | Formula |
|---|---|
| `loc_per_ac` | `loc_eoy / AC` (lines of code per contributor) |
| `cve_per_ac` | `cve_count_5y / AC` (CVEs per contributor) |
| `nni_per_ac` | `net_new_issues_5y / AC` (net-new issues per contributor) |
| `net_new_issues_5y` | `issues_opened_5y − issues_closed_5y` |
| `issue_close_ratio` | `issues_closed_5y / issues_opened_5y` (blank if 0 opened) |

### Issue trend (OLS)

`slope_opened` / `slope_closed` are the OLS slopes of the yearly opened/closed
counts over 2021–2025. `issue_trend_score = (slope_closed − slope_opened) / mean_opened`
— a volume-normalised measure of whether the maintainers are closing the gap
(positive) or falling behind (negative). It is emitted only when mean opened
volume ≥ 1, so low-traffic repos don't produce noisy slopes.

### The percentiles (`_p`)

Each metric is turned into a **worst-pinned CDF risk percentile** (0–100, direction-aware):
for a higher-is-worse axis, `P = 100 · #{vⱼ ≤ vᵢ} / n`; the worst value maps to
exactly 100, the best to `≥ 100/n > 0`, so a geometric mean never collapses to 0.
A constant axis carries no signal and yields blank.

| Column | Basis | Direction | In score? |
|---|---|---|---|
| `loc_per_ac_p` | `loc_per_ac` | higher → higher risk | **yes** |
| `cve_per_ac_p` | `cve_per_ac` | higher → higher risk | **yes** |
| `nni_per_ac_p` | `nni_per_ac` | higher → higher risk | **yes** |
| `issue_close_ratio_p` | `issue_close_ratio` | lower → higher risk | info-only |
| `issue_trend_score_p` | `issue_trend_score` | lower → higher risk | info-only |

### The score

```
score = geometric_mean(loc_per_ac_p, cve_per_ac_p, nni_per_ac_p)
```

An integer 0–100, **higher = more workload risk**, floored at 1. It is blank
unless all three component percentiles are present (i.e. LOC, CVE, NNI, and AC > 0
all exist). `issue_close_ratio_p` and `issue_trend_score_p` are informational —
they describe backlog dynamics but are **not** scoring inputs.

## Output

### `data/risk/workload.csv` (per-dimension build)

25 columns, one row per risk repo.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `repo_age_years` | years from `created_at` to EOY 2025 (1 dp) |
| `active_contributors_git_5y` | AC denominator (from concentration.csv) |
| `openssf_maintained` | Scorecard "Maintained" sub-check (0–10) |
| `has_issues` | GH /repos issues-enabled flag |
| `push_cadence_years` | count of window years with ≥1 commit (0–5) |
| `pushed_at` | last push timestamp (ISO 8601) |
| `issues_opened_5y` | issues opened, summed over 2021–2025 |
| `issues_closed_5y` | issues closed, summed over 2021–2025 |
| `issue_close_ratio` | `closed_5y / opened_5y` (3 dp) |
| `issue_close_ratio_p` | percentile of `issue_close_ratio` (info-only) |
| `net_new_issues_5y` | `opened_5y − closed_5y` |
| `slope_opened`, `slope_closed` | OLS slopes of yearly counts (2 dp) |
| `issue_trend_score` | vol-normalised `slope_closed − slope_opened` |
| `issue_trend_score_p` | percentile of `issue_trend_score` (info-only) |
| `loc_per_ac` | LOC per active contributor |
| `loc_per_ac_p` | risk percentile of `loc_per_ac` |
| `cve_per_ac` | CVEs per active contributor |
| `cve_per_ac_p` | risk percentile of `cve_per_ac` |
| `nni_per_ac` | net-new issues per active contributor |
| `nni_per_ac_p` | risk percentile of `nni_per_ac` |
| `score` | **workload-risk score** (geom-mean of the three per-AC `_p`) |
| `fetched_at` | source `repos.csv` fetch timestamp |

### `data/risk/risk.csv` (aggregate)

`aggregate_risk.py` whitelists **only** this component's `score`, carrying it in
as the `workload` column (everything else stays in `workload.csv`). The
aggregate header is `repo, repo_id, concentration, complexity, security,
funding, workload, score`, where the final `score` is the geometric mean of the
present component scores.

## Coverage

See [docs/stats.md → Risk → Workload](../stats.md#workload) for current per-signal coverage over the top repos and the score distribution.

Repos without a `score` are those missing one of the three per-AC inputs (most often
`AC = 0` or a missing LOC/CVE/issue figure).

## Limitations

- **AC = 0 / missing kills the score.** The whole component is per-AC, so a repo
  with no windowed contributors (e.g. archived, mirror-only, or git-clone
  failure) gets blank per-AC values and no `score`.
- **Issues only when enabled.** `issues.csv` is fetched only for repos that have
  issues enabled and were reachable; an absent repo stays blank (never 0), so
  `nni_per_ac` — and therefore `score` — is missing for those repos rather than
  optimistically low.
- **Upstream-dependent coverage.** Because it reads `complexity.csv`,
  `security.csv`, and `concentration.csv`, any repo those builders couldn't
  score (missing LOC, CVE, or AC) also drops out of the workload score. Workload
  coverage can never exceed the intersection of the three upstream builds.
- **`issue_trend_score` is sparse** — it needs ≥1 mean opened/year to be
  meaningful, so quiet repos carry no trend. It is info-only and never enters the
  score, so this sparsity does not reduce `score` coverage.
