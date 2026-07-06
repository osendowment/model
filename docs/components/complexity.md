# Complexity (risk component)

How large and hard-to-audit is a project's codebase? The complexity component
analyses a pinned end-of-year snapshot of each repo's default branch — lines of
code (scc), per-function McCabe and cognitive complexity (lizard), and 5-year
churn-weighted hotspots (Tornhill) — and distils them into one **complexity-risk
score (`score`)** that feeds `data/risk/risk.csv`. Higher = larger / harder to
maintain.

Scope: the valid class-A top repos in the risk pipeline — GitHub + GitLab,
archived included (counts in [stats.md → Risk](../stats.md#risk); see
[value.md](../value.md)). Build step: `src/risk/build_complexity.py`.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[EOY]` = the scc/lizard analysis of the last commit on the default branch at
the end of the chosen **snapshot year** (a year in the settings window, recorded
per-repo in `loc_year`); `[2021–2025]` = a 5-year window. Raw signals are fetched
per-source under `data/sources/`; derived columns are computed by
`build_complexity.py`.

```
Complexity  → data/risk/complexity.csv  (one row per risk-scope repo)
│
├── scc  (one sparse checkout, sha-pinned)
│   ├── loc_eoy                 ← scc.loc                 (total lines)            [EOY]
│   ├── sloc_eoy                ← scc.sloc                (source lines)           [EOY]
│   ├── scc_complexity_eoy      ← scc.complexity          (cyclomatic total)       [EOY]
│   └── scc_density_eoy         ← scc.complexity_density  (complexity per line)    [EOY]
│
├── lizard  (same checkout, sha-pinned, mainline-corrected)
│   ├── cyclomatic_total/avg/max ← lizard cyclomatic (per-function McCabe)         [EOY]
│   └── cognitive_total/avg/max  ← lizard cognitive complexity                     [EOY]
│
├── churn / hotspot  (Tornhill: bug-prone = high churn ∩ high complexity)
│   ├── churn_5y_total          ← git churn (added+deleted, bare clone)            [2021–2025]
│   ├── hotspot_raw             ← derived (churn × scc_complexity_eoy, linear)     [EOY×5y]
│   ├── hotspot_log             ← derived (log10(churn+1) × log10(complexity+1))   [EOY×5y]
│   └── hotspot_log_p           ← derived (risk percentile of hotspot_log)         [EOY×5y]
│
├── loc_year                    ← snapshot year used (real year; dated fallback)   [EOY]
│
├── informational percentiles   ← derived (risk percentiles, higher = riskier)
│   ├── scc_complexity_eoy_p · cognitive_max_p · churn_5y_total_p · hotspot_log_p
│
└── score  (the score)          ← geometric mean of loc_eoy_p, cyclomatic_max_p   [EOY]
    └─ carried into risk.csv as the column `complexity`
```

## How It Works

1. **Collect** — one unified fetcher (`fetch_sha_metrics.py`) resolves the
   end-of-year sha, sparse-clones **once**, and runs scc + both lizard passes on
   that single checkout, writing scc (loc + complexity) and lizard (cyclomatic +
   cognitive); a separate churn fetcher (audit-only — run by hand, not by the
   pipeline, since churn feeds no score) adds 5-year churn. Each is keyed on a
   sha taken from `commits-years.csv` (per-year `last_sha`).
2. **Pick the snapshot sha** — for each repo `build_complexity.py` walks **every
   available snapshot year newest→oldest** and picks the most-recent whose
   `last_sha` has scc `loc > 0`. This spans the settings window (2025→2021) and,
   for dormant repos, a **dated pre-window fallback** (e.g. 2020) recorded by
   `resolve_head` — the latest default-branch commit capped at the last complete
   year, so every repo with code resolves a dated snapshot. The chosen year is
   recorded in `loc_year`. The row is empty only when **no** sha has analysable
   code (an empty repo with `loc = 0`).
3. **Derive** — map scc/lizard long rows for the chosen `(repo, sha)` into the
   `_eoy` columns, join 5-year churn, and fold the hotspot scores.
4. **Score** — `score` = geometric mean of the LOC and per-function
   cyclomatic-max risk percentiles (`loc_eoy_p`, `cyclomatic_max_p`).
5. **Aggregate** — `aggregate_risk.py` carries **only** this component's `score`
   into `risk.csv` as the column `complexity`.

### The mainline-sha correction

GitHub's fork-network leak means a repo's pinned sha can occasionally point at an
**off-mainline** commit (a CI/template commit from another fork in the network),
whose tree is a tiny template — not the real codebase. The unified fetcher corrects
this: scc's `resolve_mainline_sha` walks the branch's first-parent history to the
real last mainline commit before the year cutoff, and `fetch_sha_metrics.py` applies
the same `corrected_clone_sha` once before its single checkout. Without this,
complexity would be measured on a template tree.

## Collection

A unified SHA-metrics fetcher (scc + lizard) and a churn fetcher (plus
`commits-years.csv` for the sha) feed the build. Every fetcher records the
analysed sha + a `fetched_at`, so a `0`/empty value is distinguishable from a
failed fetch.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `git/commits-years.csv` | `src/sources/git/commits_years.py` | per-(repo, year) `last_sha` + `commits` | `repo_id`, `year` |
| `git/scc.csv` | `src/sources/git/fetch_sha_metrics.py` (scc via `fetch_scc.py` helpers) | scc loc, sloc, complexity, complexity_density | `repo_id`, `sha` |
| `git/lizard.csv` | `src/sources/git/fetch_sha_metrics.py` | lizard cyclomatic_{total,avg,max} + cognitive_{total,avg,max} | `repo_id`, `sha` |
| `git/churn.csv` | `src/sources/github/fetch_churn.py` (audit-only, run by hand) | 5-year added+deleted lines (git churn, bare clone) | `repo_id` |

scc and lizard are stored long-format (one row per `(repo, sha, metric)`) and
read via `src.sources.git.long_format.read`; the build indexes them by
`(repo, sha)` so it can walk back through multiple shas per repo (an occasional
shallow/failed checkout records `loc = 0`, which is treated as "not measured" and
skipped to the next-oldest year).

### Off-mainline sha correction + the false-zero guard

The unified fetcher applies the first-parent **mainline-sha correction**
(`resolve_mainline_sha` / `corrected_clone_sha`) described above once, before its
single checkout, so scc and lizard analyse the same corrected tree (historically
the separate lizard fetchers did not). To defend against any residual mismatch,
`build_complexity._is_lizard_false_zero` guards the join: when scc found real
branching (`scc_complexity_eoy ≥ LIZARD_FALSE_ZERO_MIN_SCC_CX`, currently **5**)
but lizard reports `cyclomatic_total == 0`, lizard analysed the wrong (off-mainline,
function-free) tree, so its metrics are dropped to **MISSING** rather than a
score-deflating real `0`. A genuinely function-free repo (a pure data/config
module) has near-zero scc complexity too, so the threshold spares it. The
guard applies to **GitHub repos only**: the off-mainline artefact comes from
`corrected_clone_sha`, which is GitHub-specific — non-GitHub (`gl/…`) repos
sparse-clone the exact pinned SHA scc measured, so a lizard zero there is a
genuine function-free repo and is kept.

## Processing & scoring

### Snapshot selection

The snapshot is the last commit on the default branch at the end of the chosen
year. The walk picks the most-recent year with a usable sha (scc `loc > 0`)
across the window **and any dated pre-window fallback**; `loc_year` records the
real year (`"2025"`…`"2021"`, or an earlier year like `"2020"` for a dormant
repo), or `""` only when no sha has analysable code. A legacy `"HEAD"`
pseudo-bucket remains in the walk as an ultimate last resort (a repo with only
a HEAD pseudo-row and no dated year), but `resolve_head` records dated
snapshots, so it does not occur in current data.

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
| `loc_year` | snapshot year used (a real year — `2025`…`2021`, or a pre-window fallback year for dormant repos; `""` when no sha has analysable code; a `HEAD` pseudo-bucket survives in the code as ultimate fallback but does not occur in current data) |
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
`repo, repo_id, concentration, complexity, security, workload, risk_score`
(the overall `risk_score` is the geometric mean of the four component scores,
blank unless all four are present).

## Coverage

See [docs/stats.md → Risk → Complexity](../stats.md#complexity) for current per-signal coverage over the top repos and the score distribution.

A row is empty only when **no** sha yields analysable code — a genuinely empty
repo (GitHub `size = 0`, scc `loc = 0`), not a fetch gap (e.g.
`braveg1rl/performance-now`).

Some repos have scc but no lizard cognitive (e.g. `nodejs/node`,
`gcc-mirror/gcc`, `scipy/scipy`): they are either dropped by the false-zero
guard or in a language Lizard's cognitive parser doesn't cover, so `cognitive_max`
is missing while `cyclomatic_max` (and thus `score`) is still present.

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
- **`score` is a percentile, not a class.** It is a 0–100 risk percentile, not
  an A–D class — the risk pipeline has no class tiers; `complexity` enters
  `risk.csv` as a 0–100 score.
