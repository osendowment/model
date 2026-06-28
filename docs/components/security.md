# Security (risk component)

How exposed is a project to security failures? The security component reads two
independent signals — the project's **OpenSSF Scorecard** (lower score → more
risk) and its **count of distinct CVEs over 2021–2025** (more CVEs → more risk)
— and distils them into one **security-risk score (`score`, 0–100)** that feeds
`data/risk/risk.csv` as the column `security`. It also carries informational
signals (semgrep SAST findings, OSS-Fuzz enrollment, OpenSSF Best Practices
badge) that do **not** enter the score.

Scope: the 897 class-A value-class repos in the risk pipeline (see
[value.md](../value.md)). Build step: `src/risk/build_security.py`.

## Metrics Roadmap

Each leaf is one column with its data source and the period it represents.
`[2025 EOY]` = the snapshot pinned to each repo's latest 2025 commit (year
priority 2025→2021); `[2021–2025]` = a 5-year window; `[most recent]` = the
latest pull of that source. Raw signals are fetched per-source under
`data/sources/`; derived columns are computed by `build_security.py`.

```
Security  → data/risk/security.csv  (897 A/B risk repos)  →  risk.csv col `security`
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
├── SAST  (semgrep p/default — INFORMATIONAL, not scored)
│   ├── sast_findings_total / _p    ← semgrep p/default findings_total + pctl              [2025 EOY]
│   ├── sast_findings_error / _p    ← high-severity (ERROR) findings + pctl                [2025 EOY]
│   └── sast_findings_security / _p ← security-category findings + pctl                    [2025 EOY]
│
├── ossfuzz_enrolled          ← OSS-Fuzz projects index ("True"/"False")                   [most recent]
├── bestpractices_badge_id    ← deps.dev (OpenSSF Best Practices badge tier)               [most recent]
├── fetched_at                ← checked_at of the OpenSSF score row used                   [2025 EOY]
│
└── score  (the score)        ← derived (geometric mean of openssf_score_p, cve_score)     [composite]
    └─ carried into risk.csv as column `security`
```

## How It Works

1. **Collect** — fetchers pull raw signals into `data/sources/`: OpenSSF
   Scorecard (`git/openssf.csv`), its deps.dev mirror (`git/depsdev.csv`),
   semgrep findings (`git/semgrep.csv`), OSV CVEs (`osv/cves.csv`), OSS-Fuzz
   enrollment (`ossfuzz/projects.csv`), and the Best Practices badge
   (`depsdev/repos.csv`). Each is TTL/sha-gated so re-runs only fetch what's
   missing or stale.
2. **Snapshot-join** — the three sha-pinned long files (openssf, depsdev,
   semgrep) are keyed by `(repo, sha)`. For each repo, `build_security.py` walks
   the per-year `last_sha` priority from `commits-years.csv` (2025→2024→…→2021)
   and picks the first sha that has rows in that file. If no year matches, it
   falls back to any sha present for the repo (deterministic lexicographic
   pick). This is the same snapshot convention `build_complexity` uses.
3. **Derive** — read `openssf_score` (with the local→deps.dev fallback below),
   count distinct CVEs, read semgrep findings, set `ossfuzz_enrolled` and
   `bestpractices_badge_id`.
4. **Score** — `add_percentiles(...)` ranks the population, then `score` =
   geometric mean of `openssf_score_p` and `cve_score`.
5. **Aggregate** — `aggregate_risk.py` carries **only** this component's `score`
   into `risk.csv` as the column `security`.

### OpenSSF local → deps.dev fallback

`openssf_score` is taken from the locally-run Scorecard
(`data/sources/git/openssf.csv`, `openssf_score_source = "openssf_local"`).
When the snapshot picker finds no usable local row for a repo, the build falls
back to the **deps.dev-mirrored** Scorecard score
(`data/sources/git/depsdev.csv`, `openssf_score_source = "depsdev"`). If neither
yields a score, `openssf_score` and `openssf_score_source` are empty. (In the
current build, 893 repos use the local score and 4 fall back to deps.dev.)

Pipeline order (`src/risk/run_risk_pipeline.py`). The risk runner fetches these
sources by default (incremental — each fetcher skips data already present, so a
re-run only fills gaps); pass `--skip-fetch` to rebuild from existing data without
fetching:

```
commits-years → … → semgrep → … → cves → scorecard → depsdev → … → security-build → aggregate
```

## Collection

Eight source files feed the build. The three Git-snapshot long files
(`git/openssf.csv`, `git/depsdev.csv`, `git/semgrep.csv`) carry
`repo, repo_id, commit_sha, metric, value, checked_at` — one row per check/finding
metric per sha — and are joined on the snapshot sha. The rest join on `repo`.

| Source file (`data/sources/`) | Fetcher | Collects | Key |
|---|---|---|---|
| `value/value.csv` | (value stage) | class-A value-class scope | `repo` |
| `github/git/commits-years.csv` | `src.sources.git.commits_years` | per-(repo, year) `last_sha` — the snapshot pin | `repo`, `year` |
| `git/openssf.csv` | `src/sources/openssf/scorecard.py` | OpenSSF Scorecard `score` + 18 checks per `(repo, sha)` — see [openssf.md](../sources/openssf.md) | `repo`, `sha` |
| `git/depsdev.csv` | `src/sources/depsdev/fetch.py` | deps.dev-mirrored Scorecard `score` + checks (**fallback** when local row missing) | `repo`, `sha` |
| `git/semgrep.csv` | `src/sources/github/fetch_semgrep.py` | semgrep findings per `(repo, sha, rulepack-prefixed metric)`; locked to `p_default` | `repo`, `sha` |
| `osv/cves.csv` | `src/sources/osv/fetch_cves.py` | per-CVE rows `(repo, date, cve, package-source)` | `repo` |
| `osv/queried.csv` | `src/sources/osv/fetch_cves.py` | sidecar — repos OSV was queried for (confirms true zeros) | `repo` |
| `ossfuzz/projects.csv` | `src/sources/ossfuzz/fetch_ossfuzz_data.py` | OSS-Fuzz enrollment — see [ossfuzz.md](../sources/ossfuzz.md) | `github_repo` |
| `depsdev/repos.csv` | `src/sources/depsdev/fetch.py` | non-sha enrichment: `bestpractices_badge_id` | `repo` |

## Processing & scoring

### CVE counting (distinct ids, 5-year window)

Each row in `osv/cves.csv` is one `(repo, cve, package-source)` tuple — multiple
package mappings can produce duplicate `(repo, cve)` pairs, so the build
**dedupes on the CVE id within a repo**. The date filter keeps only CVEs whose
`date[:4]` falls in 2021–2025. Resolution is three-way: a count if the repo
appears in `cves.csv`; `0` if it's absent but present in `queried.csv` (a
confirmed zero); `""` if it was never queried (unknown — keeps a failed/skipped
fetch from masquerading as zero).

### Semgrep findings (locked to `p_default`, info-only)

The build reads only the `p_default.` rulepack prefix from `semgrep.csv`,
surfacing three counts at the snapshot sha — `findings_total` (all),
`findings_error` (high-severity ERROR only), `findings_security`
(security-category only) — each with an `_p` percentile. **These `sast_*_p`
percentiles are informational and are NOT inputs to `score`.**

### The percentiles (`_p`)

`add_percentiles(...)` computes direction-aware population percentiles
(0–100) over all 897 repos:

| Column | Basis | Direction (`asc`) |
|---|---|---|
| `openssf_score_p` | `openssf_score` | `False` — **lower** Scorecard score → **higher** risk pctl |
| `cve_score` | `cve_count_5y` | neutral-anchored — **0 CVEs → 50** neutral; **≥1** ranked into **(50,100]**, worst → 100 (not a plain `asc` percentile) |
| `sast_findings_total_p` | `sast_findings_total` | `True` — info only |
| `sast_findings_error_p` | `sast_findings_error` | `True` — info only |
| `sast_findings_security_p` | `sast_findings_security` | `True` — info only |

### How `score` composes

```
score = geometric_mean(openssf_score_p, cve_score)
```

`composite_cols = ["openssf_score_p", "cve_score"]`. `score` is populated
**only when both `openssf_score` and `cve_count_5y` are present**; otherwise it
is `""`. The geometric mean means a repo that is bad on *either* axis (low
Scorecard or many CVEs) carries elevated risk. Because ~78% of risk-scope repos
have zero CVEs and thus share `cve_score = 50` (neutral), for the majority
`score` effectively tracks the OpenSSF axis, with the CVE axis re-ranking only
the minority that carry CVEs.

## Output

### `data/risk/security.csv` (per-dimension build)

17 columns, one row per risk repo. Per-signal timestamps stay in each source
file; `fetched_at` here is the `checked_at` of the OpenSSF score row that was used.

| Column | Description |
|---|---|
| `repo`, `repo_id` | identity |
| `openssf_score` | OpenSSF Scorecard score (0–10), local or deps.dev mirror |
| `openssf_score_source` | `openssf_local` \| `depsdev` \| `""` |
| `cve_count_5y` | distinct CVE ids 2021–2025 (`0` confirmed-zero; `""` unknown) |
| `ossfuzz_enrolled` | `"True"`/`"False"` — enrolled in OSS-Fuzz |
| `sast_findings_total` / `_p` | semgrep p/default total findings + pctl (info) |
| `sast_findings_error` / `_p` | high-severity (ERROR) findings + pctl (info) |
| `sast_findings_security` / `_p` | security-category findings + pctl (info) |
| `bestpractices_badge_id` | `passing` \| `silver` \| `gold` \| `in_progress` \| `""` |
| `openssf_score_p` | risk pctl of `openssf_score` (lower-is-worse) |
| `cve_score` | neutral-anchored CVE risk score: 0 → 50, ≥1 ranked into (50,100] |
| `score` | **security-risk score** (geom-mean of the two `_p`; `""` if either missing) |
| `fetched_at` | `checked_at` of the OpenSSF score row used |

### `data/risk/risk.csv` (aggregate)

`aggregate_risk.py` carries **only this component's `score`** into `risk.csv`,
under the column name `security` — every other column above stays in
`security.csv`. The full `risk.csv` schema is:

```
repo, repo_id, concentration, complexity, security, funding, workload, score
```

where `security` is this component's `score`, and the final `score` is the
geometric mean of the present component scores.

## Coverage

Of the 897 A/B risk repos:

| Signal | Repos | % |
|---|---:|---:|
| `openssf_score` present | 897 | 100.0% |
| — via local Scorecard | 893 | 99.6% |
| — via deps.dev fallback | 4 | 0.4% |
| `cve_count_5y` known | 893 | 99.6% |
| `score` populated | 893 | 99.6% |
| semgrep SAST findings present | 884 | 98.6% |
| OSS-Fuzz enrolled | 130 | 14.5% |
| Best Practices badge (any tier) | 30 | 3.3% |

CVE distribution: 695 repos with zero CVEs, 198 with ≥1 (max 10,602). Best
Practices badge tiers: 18 `passing`, 10 `in_progress`, 1 `gold`, 1 `silver`.
`score` quartiles: p25 **39** · p50 **53** · p75 **63** (max 94).

## Limitations

- **Two-axis score.** Only `openssf_score_p` and `cve_score` enter `score`.
  Semgrep SAST, OSS-Fuzz, and the Best Practices badge are collected and
  surfaced but **not scored** — they are context, not inputs.
- **CVE mapping is package-name-bound.** CVE counts depend on OSV mapping a CVE
  to the repo via its published package names. C/Debian-mapped repos with
  package-name mismatches under-count (e.g. `cpython`→0, `linux`→7); a `0`
  reflects "no mapped CVEs", not necessarily "no vulnerabilities".
- **CVE axis is coarse for the majority.** ~78% of repos have zero CVEs and
  share `cve_score = 50` (the neutral baseline), so for them `score` mostly
  tracks the OpenSSF axis; the CVE axis only re-ranks the ~22% that carry CVEs.
- **Snapshot pinning, not live.** The Scorecard/semgrep signals are pinned to
  the repo's latest in-window commit (2025→2021), not re-run live, so they
  reflect the snapshot sha rather than `HEAD`.
- **`score` is not a class.** It's a 0–100 risk percentile — the risk pipeline
  has no A–D class tier; `security` enters `risk.csv` as a 0–100 score, not a
  `security_class` column.
