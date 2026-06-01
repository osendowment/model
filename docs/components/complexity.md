# Complexity (risk component)

How large and hard-to-audit is a project's codebase? The complexity component
analyses a pinned end-of-year snapshot of each repo's default branch — lines of
code (scc), per-function McCabe and cognitive complexity (lizard), and 5-year
churn-weighted hotspots (Tornhill) — and distils them into one **complexity-risk
score (`score`)** that feeds `data/risk/risk.csv`. Higher = larger / harder to
maintain.

Scope: the 897 A/B value-class repos in the risk pipeline (see
[value.md](../value.md)). Build step: `src/risk/build_complexity.py`.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[EOY]` = the scc/lizard analysis of the last commit on the default branch at
the end of the chosen **snapshot year** (a year in the settings window, recorded
per-repo in `loc_year`); `[2021–2025]` = a 5-year window. Raw signals are fetched
per-source under `data/sources/`; derived columns are computed by
`build_complexity.py`.

```
Complexity  → data/risk/complexity.csv  (897 A/B risk repos)
│
├── scc  (sparse checkout, sha-pinned)
│   ├── loc_eoy                 ← scc.loc                 (total lines)            [EOY]
│   ├── sloc_eoy                ← scc.sloc                (source lines)           [EOY]
│   ├── scc_complexity_eoy      ← scc.complexity          (cyclomatic total)       [EOY]
│   └── scc_density_eoy         ← scc.complexity_density  (complexity per line)    [EOY]
│
├── lizard  (sparse checkout, sha-pinned, mainline-corrected)
│   ├── cyclomatic_total/avg/max ← lizard cyclomatic (per-function McCabe)         [EOY]
│   └── cognitive_total/avg/max  ← lizard cognitive complexity                     [EOY]
│
├── churn / hotspot  (Tornhill: bug-prone = high churn ∩ high complexity)
│   ├── churn_5y_total          ← git churn (added+deleted, bare clone)            [2021–2025]
│   ├── hotspot_raw             ← derived (churn × scc_complexity_eoy, linear)     [EOY×5y]
│   ├── hotspot_log             ← derived (log10(churn+1) × log10(complexity+1))   [EOY×5y]
│   └── hotspot_log_p           ← derived (risk percentile of hotspot_log)         [EOY×5y]
│
├── loc_year                    ← which snapshot year was used (or "HEAD")        [EOY]
│
├── informational percentiles   ← derived (risk percentiles, higher = riskier)
│   ├── scc_complexity_eoy_p · cognitive_max_p · churn_5y_total_p · hotspot_log_p
│
└── score  (the score)          ← geometric mean of loc_eoy_p, cyclomatic_max_p   [EOY]
    └─ carried into risk.csv as the column `complexity`
```

## How It Works

1. **Collect** — three Git-analysis fetchers pull raw signals into
   `data/sources/`: scc (loc + complexity), lizard (cyclomatic + cognitive), and
   churn. Each is keyed on a sha taken from `commits-years.csv` (per-year
   `last_sha`).
2. **Pick the snapshot sha** — for each repo `build_complexity.py` walks the
   settings `years` window newest→oldest (2025→2021, then a `HEAD` pseudo-row for
   dormant repos) and picks the most-recent year whose `last_sha` has scc
   `loc > 0`. That year is recorded in `loc_year`. If no year yields a usable
   sha, the row is left empty — there is **no** fall-back to a stale or HEAD sha.
3. **Derive** — map scc/lizard long rows for the chosen `(repo, sha)` into the
   `_eoy` columns, join 5-year churn, and fold the hotspot scores.
4. **Score** — `score` = geometric mean of the LOC and per-function
   cyclomatic-max risk percentiles (`loc_eoy_p`, `cyclomatic_max_p`).
5. **Aggregate** — `aggregate_risk.py` carries **only** this component's `score`
   into `risk.csv` as the column `complexity`.

### The mainline-sha correction

GitHub's fork-network leak means a repo's pinned sha can occasionally point at an
**off-mainline** commit (a CI/template commit from another fork in the network),
whose tree is a tiny template — not the real codebase. The fetchers correct this:
scc's `resolve_mainline_sha` walks the branch's first-parent history to the real
last mainline commit before the year cutoff, and the lizard fetchers
(`fetch_advanced_complexity.py`, `fetch_cognitive.py`) now apply the same
`corrected_clone_sha` before checkout. Without this, complexity would be measured
on a template tree.

## Collection

Three Git-analysis fetchers (plus `commits-years.csv` for the sha) feed the
build. Every fetcher records the analysed sha + a `fetched_at`, so a `0`/empty
value is distinguishable from a failed fetch.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `github/git/commits-years.csv` | `src/sources/git/commits_years.py` | per-(repo, year) `last_sha` + `commits` | `repo`, `year` |
| `git/scc.csv` | `src/sources/git/fetch_scc.py` | scc loc, sloc, complexity, complexity_density | `repo`, `sha` |
| `git/lizard.csv` (cyclomatic) | `src/sources/github/fetch_advanced_complexity.py` | lizard cyclomatic_{total,avg,max} | `repo`, `sha` |
| `git/lizard.csv` (cognitive) | `src/sources/github/fetch_cognitive.py` | lizard cognitive_{total,avg,max} | `repo`, `sha` |
| `github/git/churn.csv` | git churn (bare clone) | 5-year added+deleted lines | `repo` |

scc and lizard are stored long-format (one row per `(repo, sha, metric)`) and
read via `src.sources.git.long_format.read`; the build indexes them by
`(repo, sha)` so it can walk back through multiple shas per repo (an occasional
shallow/failed checkout records `loc = 0`, which is treated as "not measured" and
skipped to the next-oldest year).

### Off-mainline sha correction + the false-zero guard

scc applies the first-parent **mainline-sha correction** (`resolve_mainline_sha`
/ `corrected_clone_sha`) described above; the lizard fetchers now apply the same
correction, but historically did not. To defend against any residual mismatch,
`build_complexity._is_lizard_false_zero` guards the join: when scc found real
branching (`scc_complexity_eoy ≥ LIZARD_FALSE_ZERO_MIN_SCC_CX`, currently **5**)
but lizard reports `cyclomatic_total == 0`, lizard analysed the wrong (off-mainline,
function-free) tree, so its metrics are dropped to **MISSING** rather than a
score-deflating real `0`. A genuinely function-free repo (a pure data/config
module) has near-zero scc complexity too, so the threshold spares it.

## Processing & scoring

### Snapshot selection

The snapshot is the last commit on the default branch at the end of the chosen
year. The walk picks the most-recent window year with a usable sha (scc
`loc > 0`); `loc_year` records the result (`"2025"`, …, `"2021"`, `"HEAD"` for
dormant repos, or `""` when nothing qualified).

### scc vs lizard metric mapping

| Source metric | Column |
|---|---|
| `scc.loc` | `loc_eoy` |
| `scc.sloc` | `sloc_eoy` |
| `scc.complexity` | `scc_complexity_eoy` (cyclomatic total) |
| `scc.complexity_density` | `scc_density_eoy` |
| `lizard.cyclomatic_{total,avg,max}` | `cyclomatic_{total,avg,max}` (per-function McCabe) |
| `lizard.cognitive_{total,avg,max}` | `cognitive_{total,avg,max}` |

### Hotspot folding (Tornhill)

Bug-prone code = high churn ∩ high complexity. The 5-year churn is joined with
the `_eoy` scc complexity snapshot:

| Column | Formula |
|---|---|
| `hotspot_raw` | `churn_5y_total × scc_complexity_eoy` (linear) |
| `hotspot_log` | `log10(churn+1) × log10(complexity+1)` |

`hotspot_log` is the canonical score — log-scaling tames the extreme right tail
(apache/airflow vs hukkin/tomli are 4–5 orders of magnitude apart on the linear
scale). Both are empty when either input is missing.

### The percentiles (`_p`)

`add_percentiles` turns each metric into a worst-pinned CDF **risk percentile**
within the repos that have a non-missing value — worst value → 100, higher =
riskier (`True` direction for all six specs):

| Column | Basis | In `score`? |
|---|---|---|
| `loc_eoy_p` | `loc_eoy` | **yes** |
| `cyclomatic_max_p` | `cyclomatic_max` | **yes** |
| `scc_complexity_eoy_p` | `scc_complexity_eoy` | informational |
| `cognitive_max_p` | `cognitive_max` | informational |
| `churn_5y_total_p` | `churn_5y_total` | informational |
| `hotspot_log_p` | `hotspot_log` | informational |

### How `score` composes

`score` = geometric mean of `loc_eoy_p` and `cyclomatic_max_p`
(`composite_cols`), available only when **both** component `_p`'s are present.
The geometric mean balances *size* (LOC) against *per-function intricacy*
(cyclomatic-max): a huge-but-flat repo and a small-but-gnarly repo both surface,
while a repo that is small **and** simple scores low on both and stays low. Range
0–100, higher = riskier.

## Output

### `data/risk/complexity.csv` (per-dimension build)

23 columns, one row per risk repo. No `fetched_at` — per-snapshot timestamps stay
in the source files (`scc.csv`, `lizard.csv`).

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `loc_eoy` | scc total lines of code at the snapshot |
| `sloc_eoy` | scc source lines of code |
| `scc_complexity_eoy` | scc cyclomatic-complexity total |
| `scc_density_eoy` | scc complexity per line |
| `cognitive_total` / `cognitive_avg` / `cognitive_max` | lizard cognitive complexity |
| `cyclomatic_total` / `cyclomatic_avg` / `cyclomatic_max` | lizard McCabe (per-function) |
| `loc_year` | snapshot year used (`2025`…`2021`, `HEAD`, or `""`) |
| `churn_5y_total` | 5-year added+deleted lines |
| `hotspot_raw` | `churn × complexity` (linear) |
| `hotspot_log` | `log10(churn+1) × log10(complexity+1)` |
| `hotspot_log_p` | risk percentile of `hotspot_log` (informational) |
| `loc_eoy_p` | risk percentile of `loc_eoy` (**score input**) |
| `scc_complexity_eoy_p` | risk percentile of `scc_complexity_eoy` (informational) |
| `cognitive_max_p` | risk percentile of `cognitive_max` (informational) |
| `cyclomatic_max_p` | risk percentile of `cyclomatic_max` (**score input**) |
| `churn_5y_total_p` | risk percentile of `churn_5y_total` (informational) |
| `score` | **complexity-risk score** (geom-mean of `loc_eoy_p` + `cyclomatic_max_p`) |

### `data/risk/risk.csv` (aggregate)

`aggregate_risk.py` carries **only** this component's `score`, writing it as the
column **`complexity`** (alongside the other dimensions' scores). All other
complexity columns stay in `complexity.csv`. The narrow `risk.csv` is just:
`repo, repo_id, concentration, complexity, security, funding, workload, score`
(the overall `score` is the geometric mean of the present component scores).

## Coverage

Of the 897 A/B risk repos:

| Signal | Repos | % |
|---|---:|---:|
| `loc_eoy` / `sloc_eoy` (scc) | 894 | 99.7% |
| `scc_complexity_eoy` | 894 | 99.7% |
| `cyclomatic_max` (lizard) | 894 | 99.7% |
| `cognitive_max` (lizard) | 880 | 98.1% |
| `churn_5y_total` | 870 | 97.0% |
| `hotspot_log` | 869 | 96.9% |
| `score` | 894 | 99.7% |

`score` percentiles: p25 **26** · p50 **49** · p75 **73** (min 1, max 100) — a
near-uniform spread, as expected from a percentile composite.

Snapshot-year mix (`loc_year`): 2025 = 655, 2024 = 82, 2023 = 36, 2021 = 38,
2022 = 24, `HEAD` (dormant) = 59, none = 3.

The 3 still-empty repos:

- **docutils/docutils**, **meinersbur/isl** — no `commits-years.csv` rows at all
  (no GitHub source / 404), so no sha to pin a snapshot to.
- **braveg1rl/performance-now** — has a sha but scc measured `loc = 0` (no
  analysable source), so the walk found no usable snapshot.

The 14 repos with scc but no lizard cognitive (e.g. `nodejs/node`,
`gcc-mirror/gcc`, `scipy/scipy`) are either dropped by the false-zero guard or
were not yet covered by the cognitive fetcher; `cyclomatic_max` (and thus `score`)
is still present for them.

## Limitations

- **One snapshot, not a trajectory.** Each repo contributes a single EOY snapshot
  (the most-recent usable year), so `score` is a point-in-time size/complexity
  reading, not a growth signal — the trend lives only in `churn_5y_total` and the
  hotspot columns.
- **`score` ignores cognitive + hotspot.** Only `loc_eoy_p` and
  `cyclomatic_max_p` compose the score; `cognitive_max_p`, `churn_5y_total_p`,
  and `hotspot_log_p` are informational. Cognitive complexity is the more
  human-readability-aligned metric but isn't yet a scoring input.
- **`cyclomatic_max` is a single worst function.** Per-function *max* McCabe is
  sensitive to one pathological function; a repo with one 200-branch parser and
  otherwise clean code scores as intricate. `cyclomatic_avg` (informational) is
  the steadier signal.
- **Mainline correction is best-effort.** The false-zero guard catches the common
  off-mainline lizard zero, but a partially-wrong off-mainline tree that still
  has *some* functions would pass the guard and slightly mis-measure.
- **`score` is a percentile, not a class.** It is not an A–D class; the legacy
  LOC-bucket class (A ≥ 1M, B 100K–1M, C 10K–100K, D < 10K) in
  [risk.md](../risk.md) is a separate, coarser view.
