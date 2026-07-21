# Workload (risk component)

How much maintenance burden rests on each active contributor? The workload
component normalizes three burdens — codebase size, security debt, and issue
backlog — **per active contributor (AC)**. It then folds the three per-AC
percentiles into one **workload-risk score** (`score`), which feeds
`data/risk/risk.csv` as the `workload` column.

Scope: the top repos in the risk pipeline — valid class-A repos across both
platforms (GitHub + GitLab), archived included (see [value.md](../value.md)).
Build step: `src/risk/build_workload.py`. It must run **after**
`build_complexity`, `build_security`, and `build_concentration`, because it reads
their per-dimension CSVs for the LOC, CVE, and AC inputs.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[2021–2025]` = the settings `years` window. `[EOY]` = anchored to the end of the
last complete year (2025). `[most recent]` = the latest pull of that source, with
no year selection. Raw signals are fetched per-source under `data/sources/`. The
three burden inputs come from sibling risk CSVs. `build_workload.py` computes
every derived column.

```
Workload  → data/risk/workload.csv  (the top risk repos)
│
├── Activity / liveness
│   ├── repo_age_years          ← repos.csv created_at → EOY                       [EOY]
│   ├── push_cadence_years      ← commits-years.csv (count of years with ≥1 commit) [2021–2025]
│   ├── openssf_maintained      ← openssf/checks.csv "Maintained" sub-check (0-10)  [most recent scan]
│   ├── has_issues              ← repos.csv (GH /repos)                            [most recent]
│   └── pushed_at               ← repos.csv (GH /repos)                            [most recent]
│
├── Issue backlog  (github/issues.csv + gitlab/issues.csv, merged)
│   ├── issues_opened_5y        ← issues.csv (metric=opened_issues, summed)        [2021–2025]
│   ├── issues_closed_5y        ← issues.csv (metric=closed_issues, summed)        [2021–2025]
│   ├── net_new_issues_5y       ← derived (opened_5y − closed_5y)                  [2021–2025]
│   ├── issue_close_ratio       ← derived (closed_5y / opened_5y)                  [2021–2025]
│   ├── slope_opened, slope_closed ← derived (OLS slope of yearly counts)          [2021–2025]
│   ├── issue_trend_score       ← derived (vol-normalized slope_closed − slope_opened) [2021–2025]
│   ├── issue_close_ratio_p     ← derived (percentile, info-only)                  [2021–2025]
│   └── issue_trend_score_p     ← derived (percentile, info-only)                  [2021–2025]
│
├── Per-AC burden  (AC = active_contributors_git_5y, from concentration.csv)
│   ├── active_contributors_git_5y ← concentration.csv (the AC denominator)        [2021–2025]
│   ├── dormant                 ← 1 if AC was 0 (then AC=1 assumed below)           [2021–2025]
│   ├── loc_per_ac              ← complexity.csv loc_eoy / AC                       [EOY]
│   ├── cve_per_ac             ← security.csv cve_count_5y / AC                     [2021–2025]
│   ├── nni_per_ac             ← net_new_issues_5y / AC                             [2021–2025]
│   └── loc_per_ac_p, cve_per_ac_p, nni_per_ac_p ← derived (risk percentiles)      [2021–2025]
│
├── fetched_at                 ← repos.csv fetch timestamp                          [most recent]
│
└── score  (the workload score) ← derived (geometric mean of loc_per_ac_p, cve_per_ac_p, nni_per_ac_p)  [2021–2025]
    └─ carried into risk.csv as the column `workload`
```

## How It Works

1. **Collect** — `build_workload.py` reads the raw GitHub/GitLab/Git/OpenSSF
   source CSVs (`repos.csv`, `commits-years.csv`, `openssf/checks.csv`, and both
   `github/issues.csv` + `gitlab/issues.csv`).
2. **Join other components** — `load_column_by_id(...)` pulls one column from
   each sibling risk CSV, keyed on `repo_id`: `loc_eoy` (complexity),
   `cve_count_5y` (security), and `active_contributors_git_5y` (concentration,
   the AC denominator).
3. **Derive per-AC** — divide each burden by AC to get `loc_per_ac`,
   `cve_per_ac`, and `nni_per_ac`. Also compute the issue-trend slopes and ratio.
4. **Score** — `score` = geometric mean of the three per-AC risk percentiles.
5. **Aggregate** — `aggregate_risk.py` carries **only** this component's `score`
   into `risk.csv` as the `workload` column.

Pipeline order (`src/risk/run_risk_pipeline.py`, run via `scripts/run-pipeline.sh
--from-stage risk`). Workload is the last dimension build, because it consumes
the three earlier builds:

```
concentration → complexity → security → workload → aggregate
```

## Collection

Workload reads nine inputs: the top-repo scope file, five raw source CSVs, and
three sibling risk-component CSVs. The three component CSVs are not external
fetches; they are the upstream build outputs this component depends on. Every
join is on the stable `repo_id`, so a rename never drops data.

| Input file | Fetcher / producer | Collects | Join key |
|---|---|---|---|
| `data/value/value.csv` | value pipeline | valid class-A top-repo scope (GitHub + GitLab, archived included) | `repo_id` |
| `data/sources/github/repos.csv` | `src/sources/github/fetch_repo_owner_data.py` (GH /repos) | `created_at`, `has_issues`, `pushed_at`, `fetched_at` | `repo_id` |
| `data/sources/git/commits-years.csv` | `src/sources/git/commits_years.py` (`gh/…`) · `src/sources/gitlab/commits_years.py` (`gl/…`) | per `(repo, year)` `commits` → `push_cadence_years` | `repo_id` |
| `data/sources/openssf/checks.csv` | `src/sources/openssf/extract_checks.py` (from the Scorecard raw `data.json`) | "Maintained" sub-check (0–10). Registered in `BUILDERS` as the `openssf-checks` step — a pure transform, no network | `repo_id` |
| `data/sources/github/issues.csv` | `src/sources/github/fetch_issue_metrics.py` (GH Search API) | long: `repo, repo_id, year, metric, value, fetched_at` (metric ∈ opened_issues, closed_issues) | `repo_id` |
| `data/sources/gitlab/issues.csv` | `src/sources/gitlab/fetch_issue_metrics.py` (one exact GitLab-API scan per project) | the same long schema, for `gl/…` repos | `repo_id` |
| `data/risk/complexity.csv` | `src/risk/build_complexity.py` | `loc_eoy` (codebase size) | `repo_id` |
| `data/risk/security.csv` | `src/risk/build_security.py` | `cve_count_5y` (security debt) | `repo_id` |
| `data/risk/concentration.csv` | `src/risk/build_concentration.py` | `active_contributors_git_5y` (AC denominator) | `repo_id` |

### Issues are long-format, and host-agnostic

Both issue files share one long schema, `repo, repo_id, year, metric, value,
fetched_at`, with one row per `(repo, year, metric)`. Two host-specific fetchers
write them over disjoint repo sets: GitHub Search supplies the `gh/…` rows, the
GitLab issues API the `gl/…` rows. `_load_issues_long` merges both and pivots to
`{metric: {repo_id: {year: count}}}` with no zero-backfill, so a year counts as
fetched only when its row exists.

The `issues_fetched` gate then requires **every** window year for **both**
metrics. A repo missing even one year — a failed fetch, or one never attempted —
gets every issue-derived column blank rather than `0`. This stops a fetch gap
from reading as "zero issues" and skewing the per-AC percentiles.

## Processing & scoring

### Per-AC normalization

`AC = active_contributors_git_5y` — distinct non-bot contributors who authored a
commit in 2021–2025, read from `concentration.csv`. Each burden is divided by AC.
A repo with **zero active contributors** in the window (dormant or bot-only) has
no real maintainer to spread the burden across. Rather than leave it unscored,
the builder attributes the whole burden to a single notional maintainer
(**AC = 1**) and flags the row **`dormant = 1`**. AC is genuinely missing only
when the concentration clone failed — the one case that blanks the per-AC ratios
(see the preview pipeline sheet → Risk → Concentration for coverage).

| Column | Formula |
|---|---|
| `loc_per_ac` | `loc_eoy / AC` (lines of code per contributor) |
| `cve_per_ac` | `cve_count_5y / AC` (CVEs per contributor) |
| `nni_per_ac` | `net_new_issues_5y / AC` (net-new issues per contributor) |
| `net_new_issues_5y` | `issues_opened_5y − issues_closed_5y` |
| `issue_close_ratio` | `issues_closed_5y / issues_opened_5y` (blank if 0 opened) |

### Issue trend (OLS)

`slope_opened` and `slope_closed` are the OLS slopes of the yearly opened and
closed counts over 2021–2025. Both are emitted only when `issues_opened_5y ≥ 1`.

`issue_trend_score = (slope_closed − slope_opened) / mean_opened` — a
volume-normalized measure of whether the maintainers close the gap (positive) or
fall behind (negative). It is emitted only when mean opened volume ≥ 1, so
low-traffic repos produce no noisy trend.

### The percentiles (`_p`)

`add_percentiles` turns each metric into a **worst-pinned CDF risk percentile**
(0–100, direction-aware). For a higher-is-worse axis, `P = 100 · #{vⱼ ≤ vᵢ} / n`.
The worst value maps to exactly 100 and the best to `≥ 100/n > 0`, so a geometric
mean never collapses to 0. A constant axis carries no signal and yields blank.

| Column | Basis | `higher_is_worse` | Direction | In score? |
|---|---|---|---|---|
| `loc_per_ac_p` | `loc_per_ac` | `True` | higher → higher risk | **yes** |
| `cve_per_ac_p` | `cve_per_ac` | `True` | higher → higher risk | **yes** |
| `nni_per_ac_p` | `nni_per_ac` | `True` | higher → higher risk | **yes** |
| `issue_close_ratio_p` | `issue_close_ratio` | `False` | lower → higher risk | info-only |
| `issue_trend_score_p` | `issue_trend_score` | `False` | lower → higher risk | info-only |

### The score

```
score = geometric_mean(loc_per_ac_p, cve_per_ac_p, nni_per_ac_p)
```

A 0–100 score, **higher = more workload risk**, floored at 1. A repo with no
fetched issues has a blank `nni_per_ac`. **When its LOC and CVE burdens are both
present**, `neutral_fill` sets `nni_per_ac_p` to 50 so the row still scores.

`score` is therefore blank in exactly three cases: `active_contributors_git_5y`
is missing (`ac_eff is None`, which blanks all three per-AC ratios), `loc_eoy` is
missing, or `cve_count_5y` is missing. `nni_per_ac` never gates the score on its
own, and the neutral fill never puts a lone 50 on a row that stays blank anyway.
`issue_close_ratio_p` and `issue_trend_score_p` describe backlog dynamics but are
**not** scoring inputs.

### An unreadable backlog is unknown, not empty

"No fetched issues" covers every repo whose GitHub/GitLab tracker is not where
the project's bugs are:

| Case | Column | Where the bugs really are |
|---|---|---|
| Tracker switched off | `has_issues=False` | Bugzilla, a mailing list, Gerrit — ffmpeg, git, sqlite, linux, gcc, krb5 … |
| Repo is a mirror | `canonical_url` set | the upstream's own tracker — gnutools/glibc → sourceware |

In both cases the Search API answers **0 issues for every year**, and 0 is not a
missing value. It passes the all-years coverage gate and lands `nni_per_ac = 0`,
the *best possible* value on this axis. Left alone, a project whose backlog the
model cannot read would collect a workload **discount** for being unreadable.

Both cases therefore blank the issue metrics outright and neutral-fill the
percentile. A repo whose own tracker really is empty keeps its measured zero. The
gate separates "no data" from "no backlog", and fills only the first.

## Output

### `data/risk/workload.csv` (per-dimension build)

26 columns, one row per risk repo.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `repo_age_years` | years from `created_at` to EOY 2025 (1 dp) |
| `active_contributors_git_5y` | AC denominator (from concentration.csv) |
| `dormant` | `1` if AC was 0 (AC=1 assumed for the per-AC ratios); `0` otherwise; blank if AC unknown |
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
| `issue_trend_score` | vol-normalized `slope_closed − slope_opened` |
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
workload, risk_score`, where the final `risk_score` is the geometric mean of
the four component scores — populated only when **all four** are present (a
partial geometric mean is not comparable across repos).

## Coverage

See the preview pipeline sheet → Risk → Workload for current per-signal coverage over the top repos and the score distribution.

A repo lacks a `score` only when `loc_eoy`, `cve_count_5y`, or AC is missing. A
missing issue figure is neutral-filled, and `AC = 0` is scored as dormant with
AC = 1, so neither blanks the score on its own.

## Limitations

- **Dormant repos are scored on a notional maintainer.** A repo with no windowed
  contributors (mirror-only or long-dormant — archived repos stay in scope)
  carries the whole burden on AC = 1 and is flagged `dormant = 1` rather than
  dropped. Only a genuinely missing AC, from a failed concentration clone, blanks
  the per-AC values and the `score`.
- **Issues only when enabled.** Both fetchers cover only repos that have issues
  enabled and were reachable. An absent repo's `nni_per_ac` stays blank, never 0.
  When LOC and CVE are both present, `nni_per_ac_p` is neutral-filled to 50 so
  the repo keeps a `score`: an unknown issue burden reads as median, neither
  optimistic nor punitive. If LOC or CVE is also missing, the row stays unscored.
- **Upstream-dependent coverage.** Workload reads `complexity.csv`,
  `security.csv`, and `concentration.csv`, so any repo those builders could not
  score also drops out of the workload score. Workload coverage can never exceed
  the intersection of the three upstream builds.
- **`issue_trend_score` is sparse.** It needs a mean of ≥1 opened issue per year,
  so quiet repos carry no trend. It is info-only and never enters the score, so
  this sparsity does not reduce `score` coverage.
