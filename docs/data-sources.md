# Data Sources

Every external source the pipeline consumes, and what each stage takes from
it. Details per source live in [docs/sources/](sources/); counts and coverage
live in the preview pipeline sheet.

Value defines the core (class-A repos). Risk and Eligibility score that same
core. See [docs/value.md](value.md#pipeline-overview).

| Source | Value | Risk | Eligibility |
|--------|-------|------|-------------|
| <img src="https://www.google.com/s2/favicons?domain=npmjs.com&sz=16" width="16"> [npm](sources/npm.md) | downloads, dep tree, package→repo resolution | CVE package mapping (via OSV) | registry license; funding field; deprecation EOL |
| <img src="https://www.google.com/s2/favicons?domain=pypi.org&sz=16" width="16"> [pypi](sources/pypi.md) | downloads, dep tree, project URLs | CVE package mapping (via OSV) | registry license; funding field; yank EOL |
| <img src="https://www.google.com/s2/favicons?domain=crates.io&sz=16" width="16"> [crates](sources/crates.md) | downloads, dep tree, repo/homepage URLs | CVE package mapping (via OSV) | registry license; yank EOL |
| <img src="https://www.google.com/s2/favicons?domain=debian.org&sz=16" width="16"> [debian](sources/debian.md) | install-count proxy, deps, C/C++ classification | — | — |
| <img src="https://www.google.com/s2/favicons?domain=brew.sh&sz=16" width="16"> [homebrew](sources/homebrew.md) | install-count proxy, deps, homepage URLs | — | SPDX license for cpp; `disable!`/`deprecate!` EOL |
| <img src="https://www.google.com/s2/favicons?domain=isocpp.org&sz=16" width="16"> [cpp](sources/cpp.md) (derived rollup) | blended downloads score, dep tree, PageRank classes | Debian package list for OSV queries | license join; `eol.csv` verdicts |
| <img src="https://www.google.com/s2/favicons?domain=github.com&sz=16" width="16"> [github](sources/github.md) | repo identity/validation, stars, `canonical_url` | SHA anchors, issue metrics | `archived` flag; license fallback; Sponsors + FUNDING.yml |
| <img src="https://www.google.com/s2/favicons?domain=gitlab.com&sz=16" width="16"> [gitlab](sources/gitlab.md) | repo identity/validation, `gl/` repo_id, stars | SHA anchors, issue metrics, Scorecard runs | `archived` flag; license fallback |
| <img src="https://www.google.com/s2/favicons?domain=git-scm.com&sz=16" width="16"> [git](sources/git.md) (local clones) | `ls-remote` URL validation → `git_valid` | complexity (LOC, cyclomatic, cognitive), concentration (bus factor/HHI), workload cadence | — |
| <img src="https://www.google.com/s2/favicons?domain=ecosyste.ms&sz=16" width="16"> [ecosystems](sources/ecosystems.md) | `eco_crit` criticality, repo-URL backfill, maintainers | — | — |
| <img src="https://www.google.com/s2/favicons?domain=openssf.org&sz=16" width="16"> [openssf](sources/openssf.md) | criticality score → `openssf_crit` | Scorecard score → security | — |
| <img src="https://www.google.com/s2/favicons?domain=osv.dev&sz=16" width="16"> [osv](sources/osv.md) | — | CVE counts per repo (security) | — |
| <img src="https://www.google.com/s2/favicons?domain=deps.dev&sz=16" width="16"> [depsdev](sources/depsdev.md) | — | fallback Scorecard; Best Practices badge | — |
| <img src="https://www.google.com/s2/favicons?domain=google.github.io&sz=16" width="16"> [ossfuzz](sources/ossfuzz.md) | C/C++ whitelist, `main_repo` URLs | fuzzing enrollment (security) | — |
| <img src="https://www.google.com/s2/favicons?domain=repology.org&sz=16" width="16"> [repology](sources/repology.md) | Debian↔Homebrew name unification, upstream URLs | — | formula lookup for cpp EOL |
| <img src="https://www.google.com/s2/favicons?domain=opensource.org&sz=16" width="16"> [osi](sources/osi.md) | — | — | OSS-approved license set → `oss` |
| <img src="https://www.google.com/s2/favicons?domain=spdx.org&sz=16" width="16"> [spdx](sources/spdx.md) | — | — | license id catalogue + FSF-libre flag → `oss` |
| <img src="https://www.google.com/s2/favicons?domain=endoflife.date&sz=16" width="16"> [endoflife](sources/endoflife.md) | — | — | product EOL cycles → `eol` → `active` |
| <img src="https://www.google.com/s2/favicons?domain=apache.org&sz=16" width="16"> [funding](sources/funding.md) ([foundation rosters](sources/foundations.md)) | — | — | foundation backing → `nonprofit`, `intent` |
| <img src="https://www.google.com/s2/favicons?domain=floss.fund&sz=16" width="16"> [floss-fund](sources/floss-fund.md) | — | — | `funding.json` manifests → `intent` |
| <img src="https://www.google.com/s2/favicons?domain=opencollective.com&sz=16" width="16"> [opencollective](sources/opencollective.md) | — | — | OC presence + budgets → `intent`, funding score |

One `data/sources/` folder is not a pipeline input: `ossinsight/` is a legacy
cache, and no builder reads it. The pipeline accepts no LLM-generated data.
