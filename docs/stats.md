# Pipeline Statistics

**Single source of truth for every pipeline count, funnel, coverage, and
distribution figure.** Methodology pages (`value.md`, `risk.md`, component/source
docs) describe *how* a metric is built and link here for *how many*.

- Computed from the live CSVs by `scripts/stats.py`; counts reflect the last run.
- Refresh: `scripts/stats.py --markdown` · dashboard: `scripts/stats.py` · drift gate: `scripts/stats.py --check`.

- [Value](#value) — funnel, class distribution, repo identity coverage
- [Risk](#risk) — per-component coverage funnels over the top repos
- [Eligibility](#eligibility) — license / activity / funding-flag coverage and the eligible rollup


## Value

How the package universe narrows from raw registries to the per-repo
`data/value/value.csv`. Methodology: [value.md](value.md) + component docs.

### Per-ecosystem value funnel

Packages remaining after each Value stage, plus the share with a known upstream repo.

| Metric | npm | pypi | crates | cpp | Total | Comment |
|---|--:|--:|--:|--:|--:|---|
| Top packages | 5,765 | 2,460 | 3,719 | 1,329 | 13,273 | Representing 95% of downloads per eco |
| **After dep tree** | **6,370** | **3,139** | **6,218** | **1,639** | **17,366** | Extended via own dependencies |
| Git URL | 6,318 | 2,929 | 6,137 | 1,311 | 16,695 | Any git host |
| GitHub repo | 6,310 | 2,899 | 6,009 | 530 | 15,748 | Github repository |
| Git % | 99.6% | 94.8% | 93.6% |
| **GitHub %** | **99%** | **92%** | **97%** | **32%** | **91%** | |

Note that after dep tree is de-duplicated, cpp unions the Debian + Homebrew graphs, Repology-canonicalised to one name each, and leave only C/C++ packages.

### Repo identity coverage

From the package universe down to the valid repos, per `class` and total. Each
row is classed by the repo's `class` (A/B/C). Packages → GitHub total are
appearance-level (a repo counts once per ecosystem); the distinct-repo rows
follow. **GitHub unique** counts only GitHub-hosted repos — the other 1,561
(gitlab / other-host / url-less) are the "orphans". **Valid** is host-agnostic:
a GitHub repo that resolves (API 200), a GitLab project that resolves (GitLab
API — these also get a `gl/{host}-{id}` / `gl/{id}` repo_id), *or* any other
upstream that resolves (`git ls-remote`). It is therefore measured over all
12,116 repos and can exceed the GitHub-unique count — only 738 (url-less orphans
+ dead URLs) are invalid. Archived GitHub mirrors of a live upstream stay *valid*
(they resolve). The numeric `repo_id` is GitHub + GitLab only, and
risk/eligibility scope is still GitHub-gated (`settings.json top_repos.platforms`)
— a valid non-GitHub upstream is recorded, not pulled into scope.

| Step | A | B | C | Total | Comment |
|---|--:|--:|--:|--:|---|
| Packages | 3,425 | 4,711 | 9,511 | 17,647 | package universe (after dep tree) |
| GitHub total repos | 3,387 | 4,313 | 8,348 | 16,048 | package appearances in a github group |
| GitHub unique repos | 915 | 2,739 | 6,901 | 10,555 | deduped; + 1,561 orphans = 12,116 repos |
| **Valid repos** | **947** | **2,985** | **7,446** | **11,378** | upstream resolves — github/gitlab API or non-github ls-remote; incl. archived mirrors |

### Value score coverage

`openssf_crit` / `eco_crit` / `value_score`, stamped onto `value.csv` by
`src.value.apply_criticality` (methodology in [value.md](value.md)).

| Signal | Filled | Comment |
|---|--:|---|
| `openssf_crit` | 921 | GitHub-only; valid class-A GitHub gate **915 / 915** (enforced by `pipeline_health.py`) |
| `eco_crit` | 794 | explicit flags only — 788 critical (`1`) / 6 not (`0`); a checked-but-blank flag (spack/debian cpp) is left empty, never 0 |
| `value_score` | 923 | ≥ 2 components present; 2 GitLab class-A repos scored — only those with an explicit `eco_crit`; the rest have just `top_eco_pct` and stay blank |

`value_score` range 0–100: **min 17.7 / mean 53.7 / max 81.4**.

### Repo class distribution

Cumulative-PageRank-share cutoffs: A ≤75%, B ≤95%, C rest.

*Repos* = distinct repos whose highest class is that column; the per-ecosystem rows count a repo once per ecosystem, so they over-sum.


| Metric | A | B | C |
|---|--:|--:|--:|
| npm | 570 | 1,402 | 2,411 |
| pypi | 165 | 636 | 1,711 |
| crates | 132 | 528 | 2,982 |
| cpp | 88 | 571 | 970 |
| **Repos** | **953** | **3,131** | **8,032** |
| GitHub % | 96.0% | 87.5% | 85.9% |
| Git % | 99.4% | 96.1% | 93.7% |
| Valid % | 99.4% | 95.3% | 92.7% |


## Risk

**Risk dimensions cover the 940 top repos** — the valid class-A set
(`risk_input.value_classes = ["A"]`) across both platforms (915 GitHub + 25
GitLab), **archived included** (they enter risk scope like every other stage;
archival surfaces in eligibility as `active=False`); each funnel below starts
from those 940. `risk.csv` ranks all 940 on the four scored dimensions; the
funding signals and the intent/nonprofit flags moved to the
[Eligibility](#eligibility) stage. Methodology: [risk.md](risk.md) + component
docs.

A handful of archived/stub repos (e.g. `bincode-org/bincode`,
`isaacs/inflight-deprecated-do-not-use`) have empty snapshots — scc measures 0
lines of code because the default branch was stripped to a README. These score
as measured zeros (floor percentiles) in complexity and workload rather than
staying blank, so all four dimensions and `risk_score` cover 100% of the risk
scope; the repos still surface in eligibility as `active=False`.

### Score distribution by component (scope 940)

- **Bold row** = the component's 0–100 risk score (feeds `risk.csv`); rows beneath = the **raw metric** in natural units, *before* its 0–100 percentile (percentiles are 0/25/50/75/100 by construction — useless).
- **Completeness rule:** overall `risk_score` = geomean of the four component scores, blank unless all four present (likewise each component vs its inputs); `pipeline_health.py` enforces it.
- **100%-populated:** zero-active-contributor repos score with AC=1 (flagged `dormant`) rather than abstaining.
- Highlights: median **bus factor 1**, **75% have 0 CVEs** (max 10,602).

Generated by `scripts/stats.py`.

| Component / subcomponent | Column | Min | P25 | P50 | P75 | Max |
|---|---|--:|--:|--:|--:|--:|
| **Concentration** | `score` | **1** | **42** | **75** | **95** | **100** |
| · bus factor | `bf_commits_git_5y` | 1 | 1 | 1 | 2 | 280 |
| · HHI | `hhi_commits_git_5y` | 19 | 3,060 | 5,556 | 8,987 | 10,000 |
| **Complexity** | `score` | **1** | **26** | **49** | **73** | **100** |
| · lines of code | `loc_eoy` | 0 | 365 | 2,751 | 21,612 | 36,990,782 |
| · cyclomatic max | `cyclomatic_max` | 0 | 7 | 18 | 48 | 12,556 |
| **Security** | `score` | **50** | **50** | **72** | **84** | **100** |
| · OpenSSF score (0–10) | `openssf_score` | 1.8 | 3.8 | 4.5 | 6.1 | 9.6 |
| · CVE count 5y | `cve_count_5y` | 0 | 0 | 0 | 0 | 10,602 |
| **Workload** | `score` | **2** | **38** | **56** | **70** | **97** |
| · LOC / contributor | `loc_per_ac` | 0 | 107 | 302 | 820 | 353,301 |
| · CVE / contributor | `cve_per_ac` | 0.00 | 0.00 | 0.00 | 0.00 | 14.00 |
| · net-new-issues / contributor | `nni_per_ac` | -9.93 | 0.00 | 0.31 | 1.00 | 315.00 |
| **Overall** | `risk_score` | **11** | **44** | **54** | **65** | **95** |

Sub-100% gaps below are structural (the signal genuinely doesn't exist for that
repo), not collection bugs.

### Concentration funnel

Contributor concentration ([concentration.md](components/concentration.md)) — bus
factor + HHI, each via git-clone log + GitHub `/contributors`. The git `_5y` axis
feeds the score, fully imputed → 100%.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 940 | 100% |
| bus factor / HHI (git 5y) computed | 940 | 100% |
| bus factor / HHI (GitHub) computed | 896 | 95.3% |
| **Concentration score** | **940** | **100%** |

73.8% of top repos have a git `_5y` bus factor of 1 (a single author covers ≥50%
of 5-year commits).

### Complexity funnel

Codebase complexity ([complexity.md](components/complexity.md)). scc + lizard +
git churn at the per-year EOY sha.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 940 | 100% |
| lines of code (scc) | 940 | 100% |
| cyclomatic max (lizard) | 940 | 100% |
| cognitive max (lizard) | 939 | 99.9% |
| churn 5y | 876 | 93.2% |
| **Complexity score** | **940** | **100%** |

`cognitive_*` needs a Lizard cognitive parser for the language; churn (bare
clone) is the heaviest fetch and times out on the largest mirrors (gcc, ffmpeg).

### Security funnel

Security posture ([security.md](components/security.md)). OpenSSF Scorecard +
OSV CVE counts + semgrep SAST.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 940 | 100% |
| OpenSSF score present | 920 | 97.9% |
| CVE count 5y > 0 | 208 | 22.1% |
| OSS-Fuzz enrolled | 135 | 14.4% |
| CII Best Practices badge | 30 | 3.2% |
| **Security score** | **940** | **100%** |

Score is `max(openssf_score_p, cve_score)` (worst-of, not geomean). ~78% have
zero CVEs and tie at the neutral `cve_score` (50), so their score is
`max(openssf_score_p, 50)` — pinned at the 50 floor when hygiene is good, else
driven up by the OpenSSF axis (hence the p25 = 50 pile). CVEs re-rank only the
minority that carry them, and are never diluted by an otherwise-good Scorecard.

### Workload funnel

Per-contributor burden ([workload.md](components/workload.md)) — LOC / CVE /
net-new issues per active contributor, plus issue-debt and trend.

| Step | Repos | % |
|---|---:|---:|
| input top repos | 940 | 100% |
| issues data present | 915 | 97.3% |
| per-AC ratios (loc/cve/nni) computed | 940 | 100% |
| `issue_close_ratio` computed | 836 | 88.9% |
| `issue_trend_score` computed | 632 | 67.2% |
| **Workload score** | **940** | **100%** |

- 76 dormant repos (`dormant=1`, zero active contributors) score with AC=1 — the burden on one notional maintainer — rather than blank.
- No-issue repos still score: `nni_per_ac_p` neutral-filled to 50. `issue_trend_score` needs `mean_opened_per_year ≥ 1`, so quiet repos are omitted.


## Eligibility

**Eligibility covers the 940 top repos** — the valid class-A set INCLUDING
archived repos, which surface here as `active=False` instead of being dropped.
This is the same 940-repo scope the risk stage now runs on (both stages include
archived and both platforms); the four checks per repo roll into
`data/eligibility/eligibility.csv`: `eligible = oss AND intent AND nonprofit
AND active`. Methodology: [eligibility.md](eligibility.md) +
[funding.md](components/funding.md).

### Licenses (scope 940)

Per-repo license resolution ([eligibility.md](eligibility.md)) — manual
`license` assertion in `overrides.csv` first (detection-failure fix), then
registry license (per-eco results.csv), GitHub Licensee fallback; `oss` = the
SPDX id (or any component of an SPDX expression) is OSI-approved ∪ curated
extras. Unknown (no license signal) is tracked separately from known non-OSS.

| Step | Repos | % |
|---|---:|---:|
| input top repos (incl. archived) | 940 | 100% |
| license resolved | 937 | 99.7% |
| · from override | 15 | 1.6% |
| · from registry | 893 | 95.0% |
| · from GitHub | 14 | 1.5% |
| **oss=True (OSI-approved)** | **919** | **97.8%** |
| oss=False (known non-OSS) | 10 | 1.1% |
| oss unknown (no signal) | 11 | 1.2% |

The known non-OSS split: 4 content-licensed data repos (CC0/CC-BY — free for
documents, not software OSS by this model's strict policy) and 6 on
OSI-unapproved software licenses (mit-open-group, bzip2, libtiff, SGI-B).

### Activity

`active` = not end-of-life (manual `eol` override in
`data/eligibility/overrides.csv`) AND not archived on GitHub — except
live-upstream mirrors (e.g. `bminor/glibc`), whose archived flag is on the
mirror while the real upstream is alive.

| Category | Repos | % |
|---|---:|---:|
| eol (override) | 0 | 0.0% |
| archived | 24 | 2.6% |
| archived but mirror-exempt | 2 | 0.2% |
| **active** | **918** | **97.7%** |

### Intent and nonprofit

`intent` = at least one funding signal — GitHub Sponsors (inbound or outbound), a
declared channel (`FUNDING.yml`, funding.json, npm/PyPI funding field, OpenCollective
slug), a bus-factor maintainer who is personally fundable on GitHub Sponsors
(`bf_maintainer_fundable`), or an institutional host/owner. `nonprofit` = not
company-backed (Meta, Google, Microsoft, …). See [funding.md](components/funding.md).

| Category | Repos | % |
|---|---:|---:|
| intent — any funding signal | 717 | 76.3% |
| intent — no funding signal | 223 | 23.7% |
| nonprofit — community / independent | 881 | 93.7% |
| nonprofit — company-backed | 59 | 6.3% |

The 59 company-backed repos are **kept in `eligibility.csv`** and flagged
`nonprofit=False` (they are already resourced; the flag makes them ineligible
without hiding them). The 223 repos with `intent=False` are not actively
soliciting support — a higher-priority target for outreach.

### Eligibility rollup

*sole blocker* = repos failing ONLY that check — what fixing it alone would
unlock. Missing intent is by far the binding constraint.

| Check | True | % | sole blocker |
|---|---:|---:|---:|
| oss | 919 | 97.8% | 1 |
| intent | 717 | 76.3% | 192 |
| nonprofit | 881 | 93.7% | 59 |
| active | 918 | 97.7% | 11 |
| **eligible** | **646** | **68.7%** | |
