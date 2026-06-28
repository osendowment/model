# Pipeline Statistics

**Single source of truth for every pipeline count, funnel, coverage, and
distribution figure in the model.** No other doc should carry these numbers —
methodology pages (`value.md`, `risk.md`, the component and source docs) describe
*how* a metric is built and link here for *how many*. Keeping all the quantities
in one place means there is exactly one number to refresh per pipeline run, and
no stale count hiding in a methodology page.

Counts reflect the **last pipeline run**. Every figure below is computed from the
live CSVs by `scripts/stats.py` — refresh them with
`uv run python scripts/stats.py --markdown` (rich dashboard:
`uv run python scripts/stats.py`; drift gate: `uv run python scripts/stats.py --check`).

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
| npm       | 5,765  | 6,370  | 6,370  | 6,297  | 99% | 6,305  | 99% |
| PyPI      | 2,460  | 3,139  | 3,139  | 2,821  | 90% | 2,850  | 91% |
| crates.io | 3,719  | 6,218  | 6,218  | 6,000  | 96% | 6,130  | 99% |
| C/C++     | 1,329  | 2,368  | 1,639  | 508    | 31% | 1,240  | 76% |
| **Total** | **13,273** | **18,095** | **17,366** | **15,626** | **90%** | **16,525** | **95%** |

*After dep tree* is already a de-duplicated set of unique package nodes
(`top ∪ transitive deps`, `unify_value_data.py:150`). For C/C++ that universe
unions the Debian and Homebrew dependency graphs — canonicalised to one name per
package by Repology — so a package shipped by both ecosystems is counted once.
C/C++'s *Results* (1,639) is smaller than *After dep tree* (2,368) **not** from
de-duplication but because the cpp pipeline's `is_cpp` filter then drops
language-agnostic distro packages.

C/C++ has the lowest GitHub coverage (31%) but Git coverage reaches 76% via
non-GitHub upstreams (sourceware, savannah, gitlab.gnome.org, etc.) resolved
through per-eco `git.csv`.

### Repo class distribution

`unify_value_data.py` collapses the 17,366 package rows into **12,060 repo
rows** (10,389 GitHub groups + 1,671 orphan packages kept under sequential ids so
nothing is dropped). The 3-class cumulative-PageRank-share cutoffs are A ≤75%,
B ≤95%, C rest. Counts are derived directly from `data/value/value.csv`.

| Class | npm | PyPI | crates.io | C/C++ | Strongest |
|---|---:|---:|---:|---:|---:|
| **A** | 570 | 165 | 132 | 89 | **954** |
| **B** | 1,410 | 637 | 531 | 571 | **3,145** |
| **C** | 2,418 | 1,722 | 2,890 | 962 | **7,961** |

*Strongest* is the count of repos for which the column is the highest class
achieved across any of its ecosystems (the `class` column in `value.csv`). The
per-ecosystem columns count a repo once per ecosystem it appears in, so they sum
to more than 12,060.

### Repo identity coverage (`value.csv`)

Share of the 12,060 repo rows carrying each identity field. A non-GitHub-only
project (glibc, gcc, …) is visible here with a populated `git_url` but no
`github_repo`, so it still slips out of the GitHub-keyed downstream analyses
(risk, EOL, contributor metrics).

| Field | Repos | % |
|---|---:|---:|
| `git_url` present | 11,265 | 93.4% |
| `github_repo` present (GitHub groups) | 10,389 | 86.1% |
| `valid == True` | 10,327 | 85.6% |
| orphan (no `github_repo`) | 1,671 | 13.9% |

By strongest `class` (counts and GitHub / Git / valid coverage). `valid` tracks
`github_repo` closely because validity requires a GitHub repo or mirror, so the
class-A set the Risk pipeline reads is almost entirely valid:

| Class | Repos | With GitHub | GH % | With Git | Git % | Valid | Valid % |
|---|--:|--:|--:|--:|--:|--:|--:|
| **A** | 954 | 917 | 96.1% | 950 | 99.6% | 917 | 96.1% |
| **B** | 3,145 | 2,703 | 85.9% | 2,915 | 92.7% | 2,694 | 85.7% |
| **C** | 7,961 | 6,769 | 85.0% | 7,400 | 93.0% | 6,716 | 84.4% |
| **Total** | 12,060 | 10,389 | 86.1% | 11,265 | 93.4% | 10,327 | 85.6% |

Per-ecosystem GitHub vs Git coverage, and the class-A subset that the Risk
pipeline depends on:

| Ecosystem | GitHub % | Git % (incl. non-GH) | A+B GitHub % | A+B Git % |
|---|---:|---:|---:|---:|
| npm | 99% | 99% | 100% | 100% |
| PyPI | 90% | 91% | 92% | 93% |
| crates.io | 96% | 99% | 99% | 100% |
| C/C++ | 31% | 76% | 42% | 67% |
| **Total** | **90%** | **95%** | **96%** | **97%** |

---

## Risk

**All Risk statistics cover the 891 top repos** — the valid class-A set read from
`data/value/value.csv` (`risk_input.value_classes = ["A"]` in `src/settings.json`).
`data/risk/risk.csv` holds one row per top repo: five 0–100 dimension scores plus
an overall `score`. Each dimension funnel below starts from those 891 and shows
how many top repos carry each signal. Methodology lives in [risk.md](risk.md) and
the per-dimension component docs.

### Score distribution by component

Min / P25 / P50 / P75 / Max of each component score across the top repos.
**Only the five components that feed the final score are shown** (bold rows) —
`risk.csv`'s overall `score` is the geometric mean of these per-repo
(`aggregate_risk.py`); no other dimension contributes. **Completeness rule:** a
score is calculable only if *all* its inputs are — each component score is blank
unless all its subcomponent percentiles are present, and the overall `score` is
blank unless all five component scores are present (so its 825 scored rows are
fewer than the components' counts; the 66-repo gap is the missing
workload score — repos with zero active contributors in the window).
`scripts/pipeline_health.py` enforces this. Under each **bold**
component sit its scored **subcomponent percentiles** (the `*_p` / score columns
that get geometric-meaned into that component). All are direction-aware 0–100
risk percentiles, so tie plateaus are visible: e.g. `cve_score` sits at the
neutral 50 for the ~78% with no CVE, and `oc_avg_funding_p` pins at 100 for the
~82% with no OpenCollective budget.

| Component / subcomponent | Scored | Min | P25 | P50 | P75 | Max |
|---|--:|--:|--:|--:|--:|--:|
| **Concentration** | **891** | **1** | **28** | **71** | **87** | **100** |
| · bus factor `bf_commits_git_5y_p` | 891 | 0 | 26 | 100 | 100 | 100 |
| · HHI `hhi_commits_git_5y_p` | 891 | 0 | 25 | 51 | 75 | 100 |
| **Complexity** | **891** | **1** | **26** | **49** | **73** | **100** |
| · LOC `loc_eoy_p` | 891 | 0 | 25 | 50 | 75 | 100 |
| · cyclomatic max `cyclomatic_max_p` | 891 | 0 | 26 | 51 | 75 | 100 |
| **Security** | **891** | **3** | **39** | **53** | **63** | **94** |
| · OpenSSF `openssf_score_p` | 891 | 0 | 26 | 52 | 76 | 100 |
| · CVE `cve_score` | 891 | 50 | 50 | 50 | 50 | 100 |
| **Funding** | **891** | **1** | **55** | **77** | **100** | **100** |
| · GitHub sponsors `gh_sponsorships_p` | 891 | 0 | 25 | 52 | 100 | 100 |
| · OpenCollective `oc_avg_funding_p` | 891 | 0 | 100 | 100 | 100 | 100 |
| **Workload** | **825** | **4** | **37** | **55** | **69** | **98** |
| · LOC / AC `loc_per_ac_p` | 825 | 0 | 25 | 50 | 75 | 100 |
| · CVE / AC `cve_per_ac_p` | 825 | 76 | 76 | 76 | 76 | 100 |
| · net-new-issues / AC `nni_per_ac_p` | 825 | 0 | 35 | 50 | 79 | 100 |
| **Overall `score`** | **825** | **10** | **39** | **50** | **60** | **92** |

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
| input top repos | 891 | 100% |
| bus factor / HHI (git 5y) computed | 891 | 100% |
| bus factor / HHI (GitHub) computed | 880 | 98.8% |
| **`score` present** | **891** | **100%** |

73.7% of top repos have a git `_5y` bus factor of 1 (a single author covers ≥50%
of 5-year commits).

### Complexity

Codebase complexity ([complexity.md](components/complexity.md)). scc + lizard +
git churn at the per-year EOY sha.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 891 | 100% |
| lines of code (scc) | 891 | 100% |
| cyclomatic max (lizard) | 891 | 100% |
| cognitive max (lizard) | 879 | 98.7% |
| churn 5y | 862 | 96.7% |
| **`score` present** | **891** | **100%** |

`cognitive_*` is only computed for languages with a Lizard cognitive parser;
churn is the heaviest fetch (bare clone) and times out on the largest mirrors
(gcc, ffmpeg, typescript).

### Security

Security posture ([security.md](components/security.md)). OpenSSF Scorecard +
OSV CVE counts + semgrep SAST.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 891 | 100% |
| OpenSSF score present | 891 | 100% |
| semgrep SAST present | 876 | 98.3% |
| CVE count 5y > 0 | 197 | 22.1% |
| OSS-Fuzz enrolled | 130 | 14.6% |
| CII Best Practices badge | 30 | 3.4% |
| **`score` present** | **891** | **100%** |

~78% of top repos have zero known CVEs and tie at the neutral `cve_score`
baseline (50), so for those the score is driven by the OpenSSF axis; CVEs only
re-rank the minority that carry them. The Best-Practices badge and OSS-Fuzz
enrollment are sparse by nature.

### Funding

How well-resourced a project is ([funding.md](components/funding.md)). GitHub
Sponsors (in + out), `FUNDING.yml`, OpenCollective budgets, FLOSS Fund manifests,
foundation hosting. The score is the geometric mean of the GitHub-sponsorship and
OpenCollective risk percentiles.

| Channel | Repos | % |
|---|---:|---:|
| input top repos | 891 | 100% |
| GitHub Sponsors inbound > 0 | 440 | 49.4% |
| ≥ 1 funding channel | 258 | 29.0% |
| `FUNDING.yml` present | 255 | 28.6% |
| Owner sponsors others (out > 0) | 157 | 17.6% |
| OpenCollective budget > 0 | 161 | 18.1% |
| Foundation host | 45 | 5.0% |
| funding.json (FLOSS Fund) | 6 | 0.7% |

The mass of unfunded repos (no sponsors, no OC) tie at the worst percentile —
`score` = 100 is the "no detectable funding" plateau.

#### OpenCollective

The OpenCollective budget signal narrows in two steps — declaring an
`open_collective` handle, then actually having a non-zero 5-year average budget:

| Step | Repos | % |
|---|---:|---:|
| input top repos | 891 | 100% |
| connected with OC slug (declared handle) | 161 | 18.1% |
| have > 0 average 5y budget | 161 | 18.1% |

### Workload

Per-contributor burden ([workload.md](components/workload.md)). LOC / CVE / net
new issues per active contributor, plus issue-debt and trend.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 891 | 100% |
| issues data present | 882 | 99.0% |
| per-AC ratios (loc/cve/nni) computed | 825 | 92.6% |
| `issue_close_ratio` computed | 814 | 91.4% |
| `issue_trend_score` computed | 617 | 69.2% |
| **`score` present** | **825** | **92.6%** |

~7% of the cohort gets no `score` — repos with zero active contributors in the
window, where per-maintainer ratios are undefined. A repo with LOC + CVE present
but no fetched issues still scores: its `nni_per_ac_p` is neutral-filled to 50.
`issue_trend_score` requires
`mean_opened_per_year ≥ 1`, so quiet repos are correctly omitted.
