# Eligibility Pipeline

**Question: can we actually fund it? Output: `eligible`, a boolean.**

Eligibility is the third scoring stage — Value → Risk → **Eligibility**. The
[README](../README.md#data-pipeline) summarizes all three; this page is the
next level of detail under it. The `preview` and `health` stages run after
eligibility, but they score nothing.

The stage is **fully automated**, not a manual review. A builder computes every
check from fetched sources plus the curated override files in
`data/eligibility/`.

**The stage flags; it never drops.** `eligibility.csv` holds one row per top
repo, and `data/preview/repos.csv` holds the same population. An ineligible repo
keeps its row, with `eligible=False` and the failing check both visible. Row
counts therefore stay identical across the stages. "Filtering" is a downstream
choice — filter on `eligible` when you want candidates only.

## What the stage produces

| File | Builder | Holds |
|---|---|---|
| `data/eligibility/licenses.csv` | `src.eligibility.build_licenses` | `oss` + the license it resolved |
| `data/eligibility/funding.csv` | `src.eligibility.build_funding` | `intent`, `nonprofit` + the funding signals behind them |
| `data/eligibility/active.csv` | `src.eligibility.build_active` | `active` + its two inputs |
| `data/eligibility/eligibility.csv` | `src.eligibility.build_eligibility` | the four flags, the `eligible` rollup, the completeness columns |

Column lists are in the [schema reference](#schema-reference). Coverage counts
live in the preview pipeline sheet.

### Scope — the top-repo set

Input is the top-repo set — valid class-A repos from `data/value/value.csv`
(`settings.json top_repos`: `classes = ["A"]`, `platforms = ["github",
"gitlab"]`, `git_valid == True`; `risk_input.value_classes` documents the same
scope), loaded via `load_top_repos()`. That set **includes archived repos by
default**, and every stage shares this one scope. An archived repo surfaces in
the stage output as `active=False`, so the reason for its ineligibility stays
visible rather than dropping out before the stage runs.

This class-A set is also called the **core** —
[value.md](value.md#pipeline-overview) defines it.

## The four checks

Four independent checks, each one boolean, each built from its own signals.

| Check | True when | Signals behind it | Builder |
|---|---|---|---|
| [`oss`](#oss--an-open-source-license) | the repo carries an open-source license | one SPDX string per repo, classified against the OSS-approved set | `build_licenses` |
| [`intent`](#intent--the-project-wants-to-be-funded) | the repo shows *any* intent to be funded | Sponsors, declared channels, maintainer Sponsors, institutional hosts | `build_funding` |
| [`nonprofit`](#nonprofit--no-company-is-strongly-affiliated) | no company is strongly affiliated with the project | a curated or scraped `company` host or owner | `build_funding` |
| [`active`](#active--not-eol-not-archived) | `NOT eol AND NOT archived` | the manual `eol` verdict + the GitHub `archived` flag | `build_active` |

### `oss` — an open-source license

`build_licenses` resolves one SPDX string per repo, then classifies it against
the OSS-approved set. Four sources compete, in strict priority order:

| # | Source | Where it comes from |
|---|---|---|
| 1 | manual assertion | `data/eligibility/overrides.csv` `license` — fixes a repo whose LICENSE detection fails upstream |
| 2 | registry license (primary) | each ecosystem's `results.csv` `license` column (npm registry / PyPI JSON / crates db-dump / Homebrew formulas); most common value across the repo's packages, ties alphabetical |
| 3 | GitHub license (fallback) | `data/sources/github/repos.csv` (GitHub Licensee detection) |
| 4 | GitLab license (fallback) | `data/sources/gitlab/repos.csv`, keyed by the `gl/` `repo_id` — the equivalent fallback for GitLab-hosted repos |

A source claims the slot only with a **meaningful** license. An unknown sentinel
(`noassertion` / `other` / `none`) never shadows a real detection from a later
source.

**The approved set.** `src.sources.osi.fetch_licenses` derives
`data/sources/osi/oss-licenses.csv` (90-day TTL, self-bootstrapping) from the
raw SPDX License List — `data/sources/spdx/licenses.csv`, fetched by
`src.sources.spdx.fetch_licenses` from the Linux Foundation registry that
carries both approval flags:

```
approved = isOsiApproved ∪ (isFsfLibre − content licenses) ∪ curated extras
```

FSF-libre adds genuine software licenses OSI never reviewed. Its **content**
licenses (CC-BY-*, CC0, GFDL) are excluded — free for documents, not software
OSS. The curated extras are the handful neither body reviewed (curl, blessing,
…). SPDX expressions (`mit or apache-2.0`, `mit/apache-2.0`, `apache-2.0 with
llvm-exception`, `gpl-3.0-or-later`) count as OSS when ANY component is
approved.

**Unknown stays distinguishable.** In `licenses.csv` the flag is ternary —
`True` / `False` / empty (no license signal at all: empty, `noassertion`,
`other`, `none`). The rollup treats only `oss=True` as eligible, so unknown
counts as not-OSS there.

### `intent` — the project wants to be funded

OSE supports only those who want to be supported. `intent` is True on any one
signal:

| Signal | Where it comes from |
|---|---|
| the owner's GitHub Sponsors listing is enabled | any inbound count implies it; outbound sponsoring feeds the score only, never intent |
| a declared channel | a FUNDING.yml resolved link or the file's mere presence, a repo- or org-level funding.json, an npm/PyPI funding field, a real Open Collective, a curated PayPal.me handle |
| a GitLab funding file | `data/sources/gitlab/funding-files.csv` — FUNDING.yml / funding.json probed in-repo, the GitLab twin of `github/funding-yml.csv` |
| a bus factor maintainer with a personal Sponsors listing | the fetched listings ∪ the curated `maintainer-overrides.csv` |
| an institutional host or owner | FOSS-foundation hosts (`data/sources/funding/host-by-repo.csv`), GitLab-instance hosts (`data/eligibility/gitlab-hosts.csv`), curated host/owner backing (`data/eligibility/overrides.csv`) |

Full methodology, the score formula and worked examples are in
[components/funding.md](components/funding.md).

**Why intent propagates at the owner level.** Once any repo an owner has in
scope self-declares a channel, every repo of that owner gets `intent=True`. This
mirrors GitHub's own org-`.github`/personal-Sponsors semantics.

### `nonprofit` — no company is strongly affiliated

OSE supports only nonprofit initiatives. `nonprofit` is an **exclusion rule, not
proof of nonprofit status**. It defaults True and flips False only when a
curated or scraped `company` host or owner backs the repo (`_nonprofit_flag` in
`src/eligibility/build_funding.py`). A True therefore means "no corporate backer
found", never "verified nonprofit". Company-backed repos are already resourced,
so they are ineligible but stay visible in the table.

### `active` — not EOL, not archived

OSE supports only projects that still have work ahead of them.
`active = NOT eol AND NOT archived`. `build_active` combines two signals:

| Signal | Source | Meaning |
|---|---|---|
| `eol` | `data/eligibility/overrides.csv` `eol` | the manual end-of-life verdict — `True` = project declared end-of-life; an explicit `False` pins a repo alive; empty = no verdict → alive |
| `archived` | `data/sources/github/repos.csv` | the GitHub `archived` flag |

The per-ecosystem registry EOL signals (`data/sources/<eco>/eol.csv`, from
`src.sources.<eco>.check_eol`) flag *packages* — deprecations, yanks,
endoflife.date dates. They inform the per-repo manual call.

**Archival has no exemption**: an archived repo is inactive, full stop. A
project whose canonical upstream lives off GitHub is repointed at that upstream
in `data/value/overrides.csv` (a `git_url`-only row, blank `repo` slug), so it
enters the pipeline as the live upstream rather than as the archived GitHub
mirror. glibc resolves to `gnutools/glibc` (`git_url` on GitHub,
`canonical_url` `sourceware.org/git/glibc.git`), and pixman to
`gitlab.freedesktop.org/pixman/pixman` — not to the archived `libpixman/pixman`
mirror.

## How the checks combine

`src.eligibility.build_eligibility` joins the four flags into
`data/eligibility/eligibility.csv`. The rollup is a plain AND of four booleans,
no weights:

```
eligible = oss AND intent AND nonprofit AND active
```

### Signal completeness — not part of the verdict

The same builder stamps three **signal-completeness** columns, independent of
the `eligible` verdict:

| Column | Counts |
|---|---|
| `value_comps` | how many value components (`openssf_crit`, `eco_crit`, `top_eco_pct`) carry a real (non-empty, **non-zero**) value |
| `risk_comps` | the same count over the risk components (`concentration`, `complexity`, `security`, `workload`) |
| `complete` | `value_comps ≥ 2 AND risk_comps = 4` — a repo with full coverage |

Archived repos are in the risk stage too, so they carry real risk scores and can
be `complete`. That includes empty-tree stubs, whose complexity and workload
score as measured zeros (floor percentiles) rather than staying blank.

## Stage overrides — `data/eligibility/overrides.csv`

One curated row per repo — or per org, when `repo` is an `owner/*` glob. An org
row applies to every in-scope repo under that owner that has no per-repo row; a
per-repo row always wins. Three builders share the file:

| Column | Used by | Meaning |
|---|---|---|
| `repo` | all | lowercased `owner/name` key (or `owner/*` org glob) |
| `repo_id` | all | stable GitHub id — the actual join key (rename-proof) |
| `host`, `host_type` | build_funding | legally-stewarding foundation or company, as a **domain** (`rustfoundation.org`), plus `company`/`nonprofit`. Scraped rosters instead emit a roster code (`apache`, `fsf/gnu`) — see [components/funding.md](components/funding.md) |
| `gh_user` | — | GitHub login (informational; no builder reads it) |
| `owner`, `owner_type` | build_funding | entity owning the GitHub org |
| `oc_slug` | build_funding | curated Open Collective slug; empty on a curated row = authoritative "no OC" |
| `license` | build_licenses | manual SPDX assertion (highest priority) when upstream detection fails — GitHub Licensee returns `noassertion` on bundled/dual/stacked LICENSE files, or the repo ships no standard LICENSE (e.g. node=`mit`, cpython=`python-2.0`, linux=`gpl-2.0-only`, icu=`unicode-3.0`) |
| `paypal` | build_funding | curated PayPal.me URL — a declared (unmeasured) funding channel |
| `eol` | build_active | manual end-of-life verdict (True/False/empty) |
| `reason` | — | free-text audit context for the override |

Repo-identity and validity corrections stay in `data/value/overrides.csv`. This
file is only for eligibility-stage judgments.

Two more curated files live in `data/eligibility/`:

| File | Used by | Meaning |
|---|---|---|
| `maintainer-overrides.csv` | build_funding | `login,reason` — bus factor maintainers fundable through a channel the Sponsors fetch can't see; unioned into the fundable-maintainer set |
| `gitlab-hosts.csv` | build_funding | `gitlab_host,host,host_type,reason` — a repo on an institution's OWN GitLab (salsa.debian.org → debian.org, invent.kde.org → kde.org, …) is host-backed by that institution. `gitlab.com` is deliberately absent: commercial shared hosting backs nothing |

## Schema reference

| File | Columns |
|---|---|
| `licenses.csv` | `repo, repo_id, license, license_source (override/registry/github/gitlab/""), oss` |
| `active.csv` | `repo, repo_id, eol, archived, active` |
| `funding.csv` | funding signals + score per repo (schema in [components/funding.md](components/funding.md)) |
| `eligibility.csv` | `repo, repo_id, oss, intent, nonprofit, active, eligible, value_comps, risk_comps, complete` |

## Running it

`scripts/run-pipeline.sh` is the only supported entry point — it runs
value → risk → eligibility → preview → health in order, so the preview workbook
and the health check never go stale behind a stage rebuild:

```bash
scripts/run-pipeline.sh --stage eligibility            # this stage alone
scripts/run-pipeline.sh --from-stage eligibility       # eligibility → preview → health
scripts/run-pipeline.sh --stage eligibility --list     # show this stage's steps
scripts/run-pipeline.sh --stage eligibility --skip-fetch   # build only, no fetchers
```

Per-step flags pass through to the stage runner
(`src.eligibility.run_eligibility_pipeline`), so `--list`, `--from <step>` and
`--skip-fetch` compose with stage selection.

The fetchers are all incremental / TTL-gated:

| Group | Fetches |
|---|---|
| repo state | the GitHub repo-owner refresh (archived flag + license fallback), the GitLab project refresh (GitLab license fallback) |
| license sets | the SPDX license list, the derived OSS-approved set, the per-eco license fetchers |
| EOL signals | the per-eco EOL fetchers |
| bus factor set | the contributor-commit fetch (`bf-contributors`) — serial, ahead of the funding group |
| funding intent | FUNDING.yml, the GitLab funding-file probe, npm/PyPI funding, inbound/outbound Sponsors, bus factor maintainer Sponsors, FLOSS Fund, the Open Collective reverse-map + budgets |
| institutional hosts | the FOSS-foundation roster scrapers + host matcher |

Builders then run in order: `licenses` → `active` → `funding-build` →
`aggregate`.

The `data/preview/` deliverables (`results`, `data`, `people`, `preview-xlsx`)
live in their own runner — `src.run_preview_pipeline`, the stage after this one
— since they roll up all three stages, not just eligibility.

`scripts/pipeline_health.py` (the `health` stage) verifies every stage CSV
matches its builder's current output. `scripts/stats.py` recomputes the preview
pipeline sheet coverage tables.

### The bus factor maintainer cache

`bf_maintainer_fundable` (an `intent` signal) keys off each repo's bus factor
set in `data/sources/github/contributor-commits.csv`. The `bf-contributors`
step (`src.sources.github.fetch_contributors_metrics`, 90-day per-row TTL)
writes it, serially and before the `funding` pgroup, because both
`maintainer-sponsors` and `funding-build` read it.

Do not confuse it with `data/sources/git/contributor-commits.csv` — the
git-clone contributor log that the risk stage fetches independently for the
concentration score. Details in
[components/funding.md](components/funding.md).

## What the checks do not prove

| Limit | What it means |
|---|---|
| an unknown license is not OSS | `licenses.csv` keeps unknown separate from known-non-OSS, but the rollup counts only `oss=True` as eligible |
| `nonprofit` is an exclusion | a True means "no corporate backer found", never "verified nonprofit" |
| off-platform money is invisible unless curated | a funding channel the fetchers cannot see needs an override row — see [components/funding.md](components/funding.md#limitations) |

## What the model hands over

The pipeline ends at a **ranked shortlist**, never at a funding decision.
`src.build_results` writes `data/preview/repos.csv` with one row per top repo.
Two columns turn that table into a queue:

| Column | Meaning |
|---|---|
| `score` | `sqrt(value_score × risk_score)`, the geometric mean of the value and risk scores, on the same absolute 0–100 scale as its inputs. It is computed for every row carrying both inputs, eligible or not. |
| `priority` | a dense rank (1, 2, 3 …) by `score` descending over eligible scored rows only. It is blank for an ineligible row and for any row with no `score`. The rank runs on the full-precision product, not on the displayed 2-decimal `score`. |

`priority` is the grant-selection queue. Everything after it is **manual and
outside the model**: verify each project's eligibility by hand, contact the
project leaders, then distribute the grants. The model contacts nobody and moves
no money.

Read `eligible` as *provisional*. It is the model's verdict from the signals it
can see, and manual due diligence confirms or overturns it. The README calls the
queue "potentially eligible" for that reason.
