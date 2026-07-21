# Security (risk component)

How exposed is a project to security failures? The security component reads two
independent signals: the project's **OpenSSF Scorecard** (lower score → more
risk) and its **count of distinct CVEs over 2021–2025** (more CVEs → more risk).
It distills them into one **security-risk score** (`score`, 0–100), which feeds
`data/risk/risk.csv` as the column `security`. It also carries two
informational signals — OSS-Fuzz enrollment and the OpenSSF Best Practices badge
— that do **not** enter the score.

Scope: the top repos in the risk pipeline — valid class-A repos across both
platforms (GitHub + GitLab), archived included (see
[value.md](../value.md)). Build step: `src/risk/build_security.py`.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[2025 EOY]` = the snapshot pinned to each repo's latest 2025 commit (year
priority 2025→2021); `[2021–2025]` = a 5-year window; `[most recent]` = the
latest pull of that source. Raw signals are fetched per-source under
`data/sources/`; derived columns are computed by `build_security.py`.

```
Security  → data/risk/security.csv  (class-A risk repos)  →  risk.csv col `security`
│
├── OpenSSF Scorecard
│   ├── openssf_score          ← scorecard `score` (0–10); local first, deps.dev fallback  [2025 EOY]
│   ├── openssf_score_source   ← derived ("openssf_local" | "depsdev" | "")                [2025 EOY]
│   └── openssf_score_p        ← derived (risk pctl; lower Scorecard → higher pctl)         [2025 EOY]
│
├── CVEs (OSV.dev)
│   ├── cve_count_5y           ← distinct CVE ids mapped to the repo in 2021–2025          [2021–2025]
│   └── cve_score             ← derived (0 CVEs → 50 neutral; ≥1 ranked into (50,100])     [2021–2025]
│
├── ossfuzz_enrolled          ← OSS-Fuzz projects index ("True"/"False")                   [most recent]
├── bestpractices_badge_id    ← deps.dev (OpenSSF Best Practices badge tier)               [most recent]
├── fetched_at                ← checked_at of the OpenSSF score row used                   [2025 EOY]
│
└── score  (the score)        ← derived (max / worst-of the present axes: openssf_score_p, cve_score) [composite]
    └─ carried into risk.csv as column `security`
```

## How It Works

1. **Collect** — fetchers pull raw signals into `data/sources/`: OpenSSF
   Scorecard (`git/openssf.csv`), its deps.dev mirror (`git/depsdev.csv`),
   OSV CVEs (`osv/cves.csv`), OSS-Fuzz
   enrollment (`ossfuzz/projects.csv`), and the Best Practices badge
   (`depsdev/repos.csv`). Each is TTL/sha-gated, so a re-run fetches only what
   is missing or stale.
2. **Snapshot-join** — the two sha-pinned long files (openssf, depsdev) are
   keyed by `(repo_id, sha)`, the stable numeric id, so renamed repos still
   join. `_per_year_shas` reads `commits-years.csv` and keeps only years with a
   non-empty `last_sha` and `commits > 0` **inside the settings window**
   (`min(YEARS) ≤ year ≤ max(YEARS)`, so 2021–2025), ordered 2025→2021.
   `_pick_latest` takes the first sha in that order with rows in the long file.
   If no windowed sha has rows, it falls back to the **lexicographically
   smallest** sha present for that `repo_id` — a deterministic pick, not a dated
   one.
3. **Derive** — read `openssf_score` (with the local→deps.dev fallback below),
   count distinct CVEs, set `ossfuzz_enrolled` and
   `bestpractices_badge_id`.
4. **Score** — `add_percentiles(...)` ranks the population, then `score` =
   max ("worst-of") of the present axes among `openssf_score_p` and `cve_score`.
5. **Aggregate** — `aggregate_risk.py` carries **only** this component's `score`
   into `risk.csv` as the column `security`.

### This is not `build_complexity`'s snapshot picker

The two builders resolve a snapshot differently. Do not read one page's rule
into the other.

| | `build_security` | `build_complexity` |
|---|---|---|
| Year window | clamped to `min(YEARS)`–`max(YEARS)` | **no clamp** — a pre-window year (e.g. 2020) is reachable |
| Accepts a snapshot when | the long file has any row for `(repo_id, sha)` | the scc row has `loc > 0` |
| Fallback | lexicographically smallest sha for the repo | the newest snapshot with a measured `loc` (the [empty-tree fallback](complexity.md#the-empty-tree-fallback)) |

See [complexity.md → Snapshot selection](complexity.md#snapshot-selection) for
the complexity side.

### OpenSSF local → deps.dev fallback

`openssf_score` is taken from the locally-run Scorecard
(`data/sources/git/openssf.csv`, `openssf_score_source = "openssf_local"`).
When the snapshot picker finds no usable local row for a repo, the build falls
back to the **deps.dev-mirrored** Scorecard score
(`data/sources/git/depsdev.csv`, `openssf_score_source = "depsdev"`). If neither
yields a score, `openssf_score` and `openssf_score_source` are empty.

Pipeline order (`src/risk/run_risk_pipeline.py`, run via `scripts/run-pipeline.sh
--from-stage risk`). The risk runner fetches these sources by default. Each
fetcher is incremental: it skips data already present, so a re-run fills only
gaps. Pass `--offline` to rebuild from existing data without fetching. The
runner holds **score-forming fetchers only** — the model scores nothing it does
not fetch:

```
commits-years → … → cves ∥ scorecard ∥ depsdev → … → security → workload → aggregate
```

`cves`, `scorecard` and `depsdev` share the `metrics` pgroup, so they run
concurrently after the SHA anchors are complete.

## Collection

Eight input files feed the build. The two Git-snapshot long files
(`git/openssf.csv`, `git/depsdev.csv`) share the header
`repo, repo_id, git_url, commit_sha, metric, value, checked_at` — one row per
check metric per sha — and join on `(repo_id, sha)`. The rest join on the stable
`repo_id`, so a rename never drops data. OSS-Fuzz rows with no `repo_id` fall
back to canonical-slug matching.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `value/value.csv` | (value stage) | valid class-A top-repo scope (GitHub + GitLab, archived included) | `repo_id` |
| `git/commits-years.csv` | `src.sources.git.commits_years` (`gh/…`) · `src.sources.gitlab.commits_years` (`gl/…`) | per-(repo, year) `last_sha` — the snapshot pin | `repo_id`, `year` |
| `git/openssf.csv` | `src/sources/openssf/scorecard.py` | OpenSSF Scorecard `score` + 18 checks per `(repo, sha)` — see [openssf.md](../sources/openssf.md) | `repo_id`, `sha` |
| `git/depsdev.csv` | `src/sources/depsdev/fetch.py` | deps.dev-mirrored Scorecard `score` + checks (**fallback** when local row missing) | `repo_id`, `sha` |
| `osv/cves.csv` | `src/sources/osv/fetch_cves.py` | per-CVE rows `(repo, repo_id, date, cve)` | `repo_id` |
| `osv/queried.csv` | `src/sources/osv/fetch_cves.py` | sidecar — repos OSV was queried for (confirms true zeros) | `repo_id` |
| `ossfuzz/projects.csv` | `src/sources/ossfuzz/fetch_ossfuzz_data.py` | OSS-Fuzz enrollment — see [ossfuzz.md](../sources/ossfuzz.md) | `repo_id` (slug fallback) |
| `depsdev/repos.csv` | `src/sources/depsdev/fetch.py` | non-sha enrichment: `bestpractices_badge_id` | `repo_id` |

## Processing & scoring

### CVE counting (distinct ids, 5-year window)

Each row in `osv/cves.csv` is one `(repo, repo_id, date, cve)` tuple. Several
package mappings can produce duplicate `(repo, cve)` pairs, so the build
**dedupes on the CVE id within a repo**. The date filter keeps only CVEs whose
`date[:4]` falls in 2021–2025. Resolution is three-way:

| `cve_count_5y` | Condition |
|---|---|
| a count | the repo appears in `cves.csv` |
| `0` | the repo is absent from `cves.csv` but present in `queried.csv` — a confirmed zero |
| `""` | the repo was never queried — unknown, so a failed or skipped fetch cannot read as zero |

`fetch_cves.py` corrects known package-name mismaps from the curated
`data/risk/cve-package-overrides.csv`, which replaces a repo's OSV package list
wholesale.

### The percentiles (`_p`)

`add_percentiles(...)` computes direction-aware population percentiles
(0–100) over the top repos:

| Column | Basis | `higher_is_worse` | Direction |
|---|---|---|---|
| `openssf_score_p` | `openssf_score` | `False` | **lower** Scorecard score → **higher** risk percentile |
| `cve_score` | `cve_count_5y` | — | neutral-anchored: **0 CVEs → 50**; **≥1** ranked into **(50,100]**, worst → 100 |

`cve_score` is not an `add_percentiles` axis. `floor_anchored_risk` computes it
before the percentile pass, which is why it carries no `higher_is_worse` flag.

### How `score` composes

```
score = max(openssf_score_p, cve_score)
```

The builder passes `composite_cols = ["openssf_score_p", "cve_score"]` and
`composite_fn = max_composite_any`. `score` is the max over the **present**
axes, so it is `""` only when *both* axes are missing. A repo with a CVE count
but no OpenSSF Scorecard still scores from the CVE axis alone; this is the
common GitLab case, because Scorecard's GitLab scan is a separate, gated run.
When both axes are present, the result is the strict max.

Max ("worst-of") means a repo that is bad on *either* axis carries that axis's
full risk. The two axes do **not** compound, and neither dilutes the other, so
an otherwise-good Scorecard never masks real CVEs. Most repos have zero CVEs and
therefore share `cve_score = 50`, and `openssf_score_p` clears 50 for most of
them, so for the majority `score = openssf_score_p`. The CVE axis takes over
only for the minority whose `cve_score` exceeds their OpenSSF axis.

## Output

### `data/risk/security.csv` (per-dimension build)

11 columns, one row per risk repo. Per-signal timestamps stay in each source
file; `fetched_at` here is the `checked_at` of the OpenSSF score row that was used.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `openssf_score` | OpenSSF Scorecard score (0–10), local or deps.dev mirror |
| `openssf_score_source` | `openssf_local` \| `depsdev` \| `""` |
| `cve_count_5y` | distinct CVE ids 2021–2025 (`0` confirmed-zero; `""` unknown) |
| `ossfuzz_enrolled` | `"True"`/`"False"` — enrolled in OSS-Fuzz |
| `bestpractices_badge_id` | `passing` \| `silver` \| `gold` \| `in_progress` \| `""` |
| `openssf_score_p` | risk pctl of `openssf_score` (lower-is-worse) |
| `cve_score` | neutral-anchored CVE risk score: 0 → 50, ≥1 ranked into (50,100] |
| `score` | **security-risk score** (max / worst-of the present axes; `""` only if both missing) |
| `fetched_at` | `checked_at` of the OpenSSF score row used |

### `data/risk/risk.csv` (aggregate)

`aggregate_risk.py` carries **only this component's `score`** into `risk.csv`,
under the column name `security` — every other column above stays in
`security.csv`. The full `risk.csv` schema is:

```
repo, repo_id, concentration, complexity, security, workload, risk_score
```

where `security` is this component's `score`, and the final `risk_score` is the
geometric mean of the four component scores — populated only when **all four**
are present (a partial geometric mean is not comparable across repos).

## Coverage

See the preview pipeline sheet → Risk → Security for current per-signal coverage over the top repos and the score distribution.

## Limitations

- **Two-axis score.** Only `openssf_score_p` and `cve_score` enter `score`.
  `ossfuzz_enrolled` and `bestpractices_badge_id` are collected and surfaced but
  **not scored**. They are context, not inputs.
- **CVE mapping is package-name-bound.** A CVE counts only when OSV maps it to
  the repo through a published package name. C/Debian-mapped repos with
  package-name mismatches under-count. `data/risk/cve-package-overrides.csv`
  corrects known mismaps (`python/cpython`, `torvalds/linux`), but a residual
  `0` still means "no mapped CVEs", not "no vulnerabilities".
- **CVE axis is coarse for the majority.** Most repos have zero CVEs and share
  the neutral `cve_score = 50`, so their `score` tracks the OpenSSF axis. The CVE
  axis re-ranks only the minority that carry CVEs.
- **Snapshot pinning, not live.** The Scorecard signals are pinned to the repo's
  latest in-window commit (year priority 2025→2021), never re-run live, so they
  reflect the snapshot sha rather than `HEAD`.
- **`score` is not a class.** It is a 0–100 risk score — the worst-of a
  percentile and a neutral-anchored CVE score. The risk pipeline has no A–D class
  tier, so `security` enters `risk.csv` as a score, not a `security_class`
  column.
