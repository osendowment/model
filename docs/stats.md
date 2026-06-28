# Pipeline Statistics

**Single source of truth for every pipeline count, funnel, coverage, and
distribution figure in the model.** No other doc should carry these numbers —
methodology pages (`value.md`, `risk.md`, the component and source docs) describe
*how* a metric is built and link here for *how many*. Keeping all the quantities
in one place means there is exactly one number to refresh per pipeline run, and
no stale count hiding in a methodology page.

Counts reflect the **last pipeline run**. Refresh the Value tables by
regenerating `data/value/value.csv`; refresh the Risk tables with
`uv run python scripts/coverage_report.py`.

- [Value](#value) — funnel, class distribution, repo identity coverage
- [Risk](#risk) — per-component coverage funnels over the top repos

---

## Value

How the package universe narrows from raw registries down to the unified
per-repo `data/value/value.csv`, and how complete repo identity is. Methodology
lives in [value.md](value.md) and the per-language component docs.

### Per-ecosystem value funnel

Packages remaining after each Value stage, plus the share with a known upstream
repo at the end. *Top packages* covers 95% of cumulative downloads. *After dep
tree* is `|top ∪ transitive deps|` — the universe analysed for PageRank.
*Results* keeps every node from that universe (top packages with no edges still
get a row, PageRank = 0); C/C++'s is smaller because the cpp pipeline drops
language-agnostic distro packages via an `is_cpp` filter. *GH %* counts only
github.com; *Git %* also counts gitlab, bitbucket, sourcehut, codeberg, and
`custom` hosts (savannah, sourceware, kernel.org, …).

| Ecosystem | Top packages | After dep tree | Results | With GitHub | GH % | With Git | Git % |
|-----------|-------------:|---------------:|--------:|------------:|-----:|---------:|------:|
| npm       | 5,765  | 6,370  | 6,370  | 6,281  | 99% | 6,281  | 99% |
| PyPI      | 2,460  | 3,139  | 3,139  | 1,728  | 55% | 1,728  | 55% |
| crates.io | 3,719  | 6,218  | 6,218  | 5,967  | 96% | 6,130  | 99% |
| C/C++     | 1,643  | 2,648  | 1,882  | 482    | 26% | 770    | 41% |
| **Total** | **13,587** | **18,375** | **17,609** | **14,458** | **82%** | **14,909** | **85%** |

PyPI is stuck at 55% because the BigQuery extract was github-only at fetch time.
C/C++ jumps from 26% GitHub to 41% Git because non-GitHub upstreams (sourceware,
savannah, gitlab.gnome.org, etc.) resolve via per-eco `git.csv`.

### Repo class distribution

`unify_value_data.py` collapses the 17,609 package rows into **12,117 repo
rows** (10,446 GitHub groups + 1,671 orphan packages kept under sequential ids so
nothing is dropped). The 3-class cumulative-PageRank-share cutoffs are A ≤75%,
B ≤95%, C rest. Counts are derived directly from `data/value/value.csv`.

| Class | npm | PyPI | crates.io | C/C++ | Strongest |
|---|---:|---:|---:|---:|---:|
| **A** | 574 | 165 | 133 | 89 | **959** |
| **B** | 1,418 | 641 | 534 | 571 | **3,160** |
| **C** | 2,428 | 1,726 | 2,911 | 962 | **7,998** |

*Strongest* is the count of repos for which the column is the highest class
achieved across any of its ecosystems (the `class` column in `value.csv`). The
per-ecosystem columns count a repo once per ecosystem it appears in, so they sum
to more than 12,117.

### Repo identity coverage (`value.csv`)

Share of the 12,117 repo rows carrying each identity field. A non-GitHub-only
project (glibc, gcc, …) is visible here with a populated `git_url` but no
`github_repo`, so it still slips out of the GitHub-keyed downstream analyses
(risk, EOL, contributor metrics).

| Field | Repos | % |
|---|---:|---:|
| `git_url` present | 11,322 | 93.4% |
| `github_repo` present (GitHub groups) | 10,446 | 86.2% |
| `valid == True` | 10,384 | 85.7% |
| orphan (no `github_repo`) | 1,671 | 13.8% |

Per-ecosystem GitHub vs Git coverage, and the class-A subset that the Risk
pipeline depends on:

| Ecosystem | GitHub % | Git % (incl. non-GH) | A+B GitHub % | A+B Git % |
|---|---:|---:|---:|---:|
| npm | 99% | 99% | 100% | 100% |
| PyPI | 55% | 55% | 76% | 76% |
| crates.io | 96% | 99% | 99% | 100% |
| C/C++ | 26% | 41% | 32% | **95%** |
| **Total** | **82%** | **85%** | **93%** | **96%** |

---

## Risk

**All Risk statistics cover the 893 top repos** — the valid class-A set read from
`data/value/value.csv` (`risk_input.value_classes = ["A"]` in `src/settings.json`).
`data/risk/risk.csv` holds one row per top repo: five 0–100 dimension scores plus
an overall `score`. Each dimension funnel below starts from those 893 and shows
how many top repos carry each signal. Methodology lives in [risk.md](risk.md) and
the per-dimension component docs.

Overall `risk.csv` score distribution: **p25 41 · p50 52 · p75 62**.

Median per-column coverage across the dimension builds is ~99%; every sub-100%
gap below is structural (a signal that genuinely doesn't exist for that repo),
not a data-collection bug.

### Concentration

Contributor-concentration ([concentration.md](components/concentration.md)). Bus
factor + HHI, each resolved by two methods (git-clone log and the GitHub
`/contributors` API). The git `_5y` axis feeds the score and is fully imputed, so
it is 100%-populated.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 893 | 100% |
| bus factor / HHI (git 5y) computed | 893 | 100% |
| bus factor / HHI (GitHub) computed | 880 | 98.5% |
| **`score` present** | **893** | **100%** |

Score distribution: p25 27 · p50 71 · p75 87. 73.6% of top repos have a git
`_5y` bus factor of 1 (a single author covers ≥50% of 5-year commits).

### Complexity

Codebase complexity ([complexity.md](components/complexity.md)). scc + lizard +
git churn at the per-year EOY sha.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 893 | 100% |
| lines of code (scc) | 886 | 99.2% |
| cyclomatic max (lizard) | 886 | 99.2% |
| cognitive max (lizard) | 872 | 97.6% |
| churn 5y | 761 | 85.2% |
| **`score` present** | **886** | **99.2%** |

Score distribution: p25 26 · p50 50 · p75 73. `cognitive_*` is only computed for
languages with a Lizard cognitive parser; churn is the heaviest fetch (bare
clone) and times out on the largest mirrors (gcc, ffmpeg, typescript).

### Security

Security posture ([security.md](components/security.md)). OpenSSF Scorecard +
OSV CVE counts + semgrep SAST.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 893 | 100% |
| OpenSSF score present | 888 | 99.4% |
| semgrep SAST present | 874 | 97.9% |
| CVE count 5y > 0 | 194 | 21.7% |
| CII Best Practices badge | 30 | 3.4% |
| **`score` present** | **882** | **98.8%** |

Score distribution: p25 46 · p50 64 · p75 78. ~78% of top repos have zero known
CVEs and tie on `cve_count_5y_p`, so for those the score is driven by the OpenSSF
axis. The Best-Practices badge and OSS-Fuzf enrollment are sparse by nature.

### Funding

How well-resourced a project is ([funding.md](components/funding.md)). GitHub
Sponsors (in + out), `FUNDING.yml`, OpenCollective budgets, FLOSS Fund manifests,
foundation hosting. The score is the geometric mean of the GitHub-sponsorship and
OpenCollective risk percentiles.

| Channel | Repos | % |
|---|---:|---:|
| input top repos | 893 | 100% |
| GitHub Sponsors inbound > 0 | 440 | 49.3% |
| ≥ 1 funding channel | 258 | 28.9% |
| `FUNDING.yml` present | 255 | 28.6% |
| Owner sponsors others (out > 0) | 158 | 17.7% |
| OpenCollective budget > 0 | 45 | 5.0% |
| Foundation host | 38 | 4.3% |
| funding.json (FLOSS Fund) | 6 | 0.7% |

Score distribution: p25 46 · p50 70 · p75 100. The mass of unfunded repos (no
sponsors, no OC) tie at the worst percentile — `score` = 100 is the "no
detectable funding" plateau.

#### OpenCollective

The OpenCollective budget signal narrows in two steps — declaring an
`open_collective` handle, then actually having a non-zero 5-year average budget:

| Step | Repos | % |
|---|---:|---:|
| input top repos | 893 | 100% |
| connected with OC slug (declared handle) | 48 | 5.4% |
| have > 0 average 5y budget | 45 | 5.0% |

### Workload

Per-contributor burden ([workload.md](components/workload.md)). LOC / CVE / net
new issues per active contributor, plus issue-debt and trend.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 893 | 100% |
| issues data present | 886 | 99.2% |
| per-AC ratios (loc/cve/nni) computed | 822 | 92.0% |
| `issue_close_ratio` computed | 819 | 91.7% |
| `issue_trend_score` computed | 622 | 69.7% |
| **`score` present** | **816** | **91.4%** |

Score distribution: p25 37 · p50 55 · p75 69. ~8% of the cohort gets no `score` —
a missing upstream input (complexity row, AC count, or issues fetch) blanks the
per-AC ratios. `issue_trend_score` requires `mean_opened_per_year ≥ 1`, so quiet
repos are correctly omitted.
