# Complexity (risk component)

How large and hard to audit is a project's codebase? The complexity component
analyzes a pinned end-of-year snapshot of each repo's default branch — lines of
code (scc) plus per-function McCabe and cognitive complexity (lizard) — and
reduces them to one **complexity-risk score (`score`)** in
`data/risk/risk.csv`. Higher = larger and harder to maintain.

Scope: the valid class-A top repos of the risk pipeline — GitHub + GitLab,
archived included (see [value.md](../value.md); counts in the preview pipeline
sheet → Risk). Build step: `src/risk/build_complexity.py`.

## Metrics Roadmap

One leaf per column, with its data source and the period it represents.
`[EOY]` = the scc/lizard analysis of the last commit on the default branch at
the end of the chosen **snapshot year**, recorded per repo in `loc_year`.

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
├── loc_year                    ← snapshot year used (real year; dated fallback)   [EOY]
│
├── informational percentiles   ← derived (risk percentiles, higher = riskier)
│   └── scc_complexity_eoy_p · cognitive_max_p
│
└── score  (the score)          ← geometric mean of loc_eoy_p, cyclomatic_max_p   [EOY]
    └─ carried into risk.csv as the column `complexity`
```

## How It Works

1. **Collect** — one unified fetcher (`fetch_sha_metrics.py`) resolves the
   end-of-year sha, sparse-clones **once**, and runs scc plus both lizard passes
   on that single checkout. The sha comes from the per-year `last_sha` in
   `commits-years.csv`.
2. **Pick the snapshot sha** — `build_complexity.py` walks **every available
   snapshot year newest→oldest** and takes the most-recent whose `last_sha` has
   scc `loc > 0`. The walk spans the settings window (2025→2021) and, for a
   dormant repo, a **dated pre-window fallback** year recorded by `resolve_head`
   — the latest default-branch commit, capped at the last complete year. It
   records the chosen year in `loc_year`. If **every** measured snapshot reports
   `loc = 0`, the empty-tree fallback scores the repo as a real zero. The row is
   empty only when no snapshot was measured at all.
3. **Derive** — map the scc/lizard long rows for the chosen `(repo, sha)` into
   the `_eoy` columns and percentile-rank them.
4. **Score** — `score` = geometric mean of `loc_eoy_p` and `cyclomatic_max_p`.
5. **Aggregate** — `aggregate_risk.py` carries **only** this component's `score`
   into `risk.csv`, as the column `complexity`.

### The mainline-sha correction

GitHub's fork-network leak lets a pinned sha point at an **off-mainline** commit
— a CI or template commit from another fork in the network, whose tree is a tiny
template rather than the real codebase. The fetcher corrects this before it
clones: `resolve_mainline_sha` walks the branch's first-parent history back to
the last real mainline commit before the year cutoff, and `fetch_sha_metrics.py`
applies that `corrected_clone_sha` once. Without the correction, complexity
would measure a template tree.

## Collection

The build reads one unified SHA-metrics fetcher (scc + lizard) plus
`commits-years.csv` for the sha. Every fetcher records the analyzed sha and a
`fetched_at`, so a `0` or empty value is distinguishable from a failed fetch.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `git/commits-years.csv` | `src/sources/git/commits_years.py` (+ `resolve_head.py`) for `gh/…`; `src/sources/gitlab/commits_years.py` for `gl/…` | per-(repo, year) `last_sha` + `commits` — both hosts merge-write this one file | `repo_id`, `year` |
| `git/scc.csv` | `src/sources/git/fetch_sha_metrics.py` (scc via `fetch_scc.py` helpers) | scc loc, sloc, complexity, complexity_density | `repo_id`, `sha` |
| `git/lizard.csv` | `src/sources/git/fetch_sha_metrics.py` | lizard cyclomatic_{total,avg,max} + cognitive_{total,avg,max} | `repo_id`, `sha` |

scc and lizard are stored long-format (one row per `(repo, sha, metric)`) and
read via `src.sources.git.long_format.read`. The build indexes them by
`(repo, sha)` so it can walk back through several shas per repo. A shallow or
failed checkout records `loc = 0`, which the walk treats as "not measured" and
skips to the next-oldest year.

### The lizard false-zero guard

The fetcher already applies the mainline-sha correction above, so scc and lizard
analyze the same corrected tree. `build_complexity._is_lizard_false_zero` guards
the join against any residual mismatch. It fires when scc found real branching
(`scc_complexity_eoy ≥ LIZARD_FALSE_ZERO_MIN_SCC_CX`, **5**) but lizard reports
`cyclomatic_total == 0` — lizard analyzed the wrong, function-free tree. The
guard drops all six lizard columns to **MISSING** rather than keep a
score-deflating `0`. A genuinely function-free repo (a pure data or config
module) has near-zero scc complexity too, so the threshold spares it.

The guard applies to **GitHub repos only**. The off-mainline artifact comes from
`corrected_clone_sha`, which is GitHub-specific: a `gl/…` repo sparse-clones the
exact pinned sha scc measured, so a lizard zero there is a genuine
function-free repo and is kept.

## Processing & scoring

### Snapshot selection

The snapshot is the last commit on the default branch at the end of the chosen
year. The walk picks the most-recent year with a usable sha (scc `loc > 0`)
across the window **and any dated pre-window fallback**. `loc_year` records that
real year — `"2025"`…`"2021"`, or an earlier year for a dormant repo — and `""`
only when no snapshot was measured. Snapshots are always dated: `resolve_head`
writes a dated end-of-year sha for a dormant repo, never an undated `HEAD`.

### The empty-tree fallback

The walk normally skips a snapshot whose scc row reports `loc = 0`, because a
failed or shallow checkout writes an all-zero row. But when **every** measured
snapshot of a repo reports `loc = 0`, the default branch is genuinely code-free
— an archived stub stripped back to a README (`bincode-org/bincode`,
`isaacs/inflight-deprecated-do-not-use`). Such a repo takes its newest measured
zero snapshot and scores as a **real zero** (floor percentiles) instead of
blanking the dimension. Absent lizard metrics for that sha read as measured
zeros too: a tree with zero source files has zero functions by definition. A
bogus zero can never reach this path for a repo with any healthy snapshot,
because the `loc > 0` walk wins first.

### scc vs lizard metric mapping

| Source metric | Column |
|---|---|
| `scc.loc` | `loc_eoy` |
| `scc.sloc` | `sloc_eoy` |
| `scc.complexity` | `scc_complexity_eoy` (cyclomatic total) |
| `scc.complexity_density` | `scc_density_eoy` |
| `lizard.cyclomatic_{total,avg,max}` | `cyclomatic_{total,avg,max}` (per-function McCabe) |
| `lizard.cognitive_{total,avg,max}` | `cognitive_{total,avg,max}` |

### The percentiles (`_p`)

`add_percentiles` turns each metric into a worst-pinned CDF **risk percentile**
across the repos with a non-missing value: worst value → 100, higher = riskier
(`higher_is_worse=True` for all four specs).

| Column | Basis | In `score`? |
|---|---|---|
| `loc_eoy_p` | `loc_eoy` | **yes** |
| `cyclomatic_max_p` | `cyclomatic_max` | **yes** |
| `scc_complexity_eoy_p` | `scc_complexity_eoy` | informational |
| `cognitive_max_p` | `cognitive_max` | informational |

### How `score` composes

`score` = geometric mean of `loc_eoy_p` and `cyclomatic_max_p`
(`composite_cols`), present only when **both** `_p` inputs are present. The
geometric mean balances *size* (LOC) against *per-function intricacy*
(cyclomatic-max): a huge-but-flat repo and a small-but-gnarly repo both surface,
while a repo that is small **and** simple scores low on both and stays low.

`add_percentiles` floors the result at 1, so the range is 1–100, higher =
riskier. A code-free tree lands on exactly `1.00`; that is the lowest-risk tier,
not "no risk".

## Output

### `data/risk/complexity.csv` (per-dimension build)

18 columns, one row per risk repo. No `fetched_at` — per-snapshot timestamps stay
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
| `loc_year` | snapshot year used (a real year — `2025`…`2021`, or a pre-window fallback year for dormant repos; `""` when no snapshot was measured) |
| `loc_eoy_p` | risk percentile of `loc_eoy` (**score input**) |
| `scc_complexity_eoy_p` | risk percentile of `scc_complexity_eoy` (informational) |
| `cognitive_max_p` | risk percentile of `cognitive_max` (informational) |
| `cyclomatic_max_p` | risk percentile of `cyclomatic_max` (**score input**) |
| `score` | **complexity-risk score** (geom-mean of `loc_eoy_p` + `cyclomatic_max_p`) |

### `data/risk/risk.csv` (aggregate)

`aggregate_risk.py` carries **only** this component's `score` into `risk.csv`,
as the column **`complexity`**. Every other complexity column stays in
`complexity.csv`. The narrow `risk.csv` header is
`repo, repo_id, concentration, complexity, security, workload, risk_score`;
`risk_score` is the geometric mean of the four component scores, blank unless
all four are present.

## Coverage

Per-signal coverage over the top repos and the score distribution live in the
preview pipeline sheet → Risk → Complexity.

A row is empty only when **no snapshot was measured at all** — a repo with no
usable sha, or one whose checkouts never produced an scc row. A genuinely
code-free tree (every snapshot `loc = 0`) is *not* a gap: the empty-tree fallback
scores it as a real zero.

`cognitive_max = 0` is a measurement, not a gap either. A branch-free module
(`sindresorhus/shebang-regex`, `colorjs/color-name`) has zero cognitive
complexity by definition, and lizard reports it as zero.

Lizard columns are blanked as a group. The false-zero guard sets `lz_vals = {}`,
so all six `cyclomatic_*` / `cognitive_*` columns go missing together — a repo
never has `cyclomatic_max` without `cognitive_max`.

## Limitations

- **One snapshot, not a trajectory.** Each repo contributes a single EOY
  snapshot, so `score` reads size and complexity at a point in time. It carries
  no growth signal.
- **`score` ignores cognitive complexity.** Only `loc_eoy_p` and
  `cyclomatic_max_p` compose it. `scc_complexity_eoy_p` and `cognitive_max_p`
  stay informational, although cognitive complexity aligns better with human
  readability.
- **`cyclomatic_max` is a single worst function.** One 200-branch parser makes
  an otherwise clean repo score as intricate. `cyclomatic_avg` (informational)
  is the steadier signal.
- **Mainline correction is best-effort.** The false-zero guard catches the
  common off-mainline lizard zero. A partially-wrong off-mainline tree that
  still has *some* functions passes the guard and mis-measures slightly.
- **`score` is a percentile, not a class.** The risk pipeline has no A–D class
  tiers; `complexity` enters `risk.csv` as a 1–100 score.
