# Eligibility Pipeline

Stage 3 of the pipeline (Value → Risk → **Eligibility**). Takes the top
repos and answers one question per repo: **is it eligible for funding?**

It is a **fully automated pipeline stage**, not a manual review: every check is
computed by a builder from fetched sources plus the curated override files in
`data/eligibility/`. Four independent checks, one AND-rollup — a plain AND of
four booleans, no weights. Ineligible repos stay in the table with the failing
flag visible:

```
eligible = oss AND intent AND nonprofit AND active
```

| Check | Meaning | Built by | Output |
|---|---|---|---|
| `oss` | OSS-approved license | `src.eligibility.build_licenses` | `data/eligibility/licenses.csv` |
| `intent` | shows any funding signal | `src.eligibility.build_funding` | `data/eligibility/funding.csv` |
| `nonprofit` | not company-backed | `src.eligibility.build_funding` | `data/eligibility/funding.csv` |
| `active` | not EOL, not archived | `src.eligibility.build_active` | `data/eligibility/active.csv` |

`src.eligibility.build_eligibility` joins the four flags into
`data/eligibility/eligibility.csv`. Coverage counts live in
the preview pipeline sheet.

It also stamps three **signal-completeness** columns (independent of the
`eligible` verdict): `value_comps` and `risk_comps` count how many value
components (`openssf_crit`, `eco_crit`, `top_eco_pct`) and risk components
(`concentration`, `complexity`, `security`, `workload`) carry a real
(non-empty, **non-zero**) value, and `complete = value_comps ≥ 2 AND
risk_comps = 4` flags a repo with full coverage. Archived repos are in the
risk stage too, so they carry real risk scores and can be `complete` —
including empty-tree stubs, whose complexity/workload score as measured
zeros (floor percentiles) rather than staying blank.

## Scope

Input is the top-repo set — valid class-A repos from `data/value/value.csv`
(`settings.json top_repos`: `classes = ["A"]`, `platforms = ["github",
"gitlab"]`, `git_valid == True`; `risk_input.value_classes` documents the
same scope), loaded via `load_top_repos()`, which **includes archived repos by
default** — every stage shares this one scope. Archived repos appear in the
stage output as `active=False`, so the reason for their ineligibility is
visible rather than silently dropped before the stage runs.

## Metrics Roadmap

```
Eligibility
│
├── Scope gate
│   └── value_class == A ∩ valid    ← data/value/value.csv (archived kept)
│
├── oss — License (→ licenses.csv)
│   ├── manual assertion (override) ← data/eligibility/overrides.csv `license`
│   │                                 (highest priority, detection-failure fix)
│   ├── registry license (primary)  ← per-eco results.csv `license` column
│   │                                 (npm registry / PyPI JSON / crates
│   │                                  db-dump / Homebrew formulas)
│   ├── github license (fallback)   ← data/sources/github/repos.csv (Licensee)
│   ├── gitlab license (fallback)   ← data/sources/gitlab/repos.csv
│   │                                 (GitLab-hosted repos, keyed gl/ repo_id)
│   ├── oss-approved set            ← data/sources/osi/oss-licenses.csv
│   │                                 (derived from the SPDX License List,
│   │                                  data/sources/spdx/licenses.csv:
│   │                                  isOsiApproved ∪ (isFsfLibre − content
│   │                                  licenses) ∪ curated extras;
│   │                                  90-day TTL, self-bootstrapping)
│   └── oss                         ← ternary True / False / "" (unknown);
│                                     SPDX-expression-aware
│
├── intent + nonprofit — Funding (→ funding.csv, see components/funding.md)
│   ├── GitHub Sponsors in/out, FUNDING.yml, funding.json, npm/PyPI
│   │   funding fields, bus-factor-maintainer Sponsors, Open Collective budgets
│   ├── GitLab funding files        ← data/sources/gitlab/funding-files.csv
│   │                                 (FUNDING.yml / funding.json probed in-repo —
│   │                                  the GitLab twin of github/funding-yml.csv)
│   ├── FOSS-foundation hosts       ← data/sources/funding/host-by-repo.csv
│   ├── GitLab-instance hosts       ← data/eligibility/gitlab-hosts.csv
│   │                                 (salsa.debian.org → debian.org, …)
│   └── curated host/owner backing  ← data/eligibility/overrides.csv
│                                     (+ maintainer-overrides.csv)
│
├── active — Activity (→ active.csv)
│   ├── eol                         ← data/eligibility/overrides.csv `eol`
│   │                                 (manual per-repo verdict; the per-eco
│   │                                  check_eol registry signals are its
│   │                                  advisory inputs)
│   └── archived                    ← data/sources/github/repos.csv
│                                     (no exemption — an archived repo is
│                                      inactive; a project whose canonical
│                                      upstream is off GitHub is repointed
│                                      there in data/value/overrides.csv)
│
└── Final rollup (→ eligibility.csv)
    └── eligible = oss AND intent AND nonprofit AND active
```

## The four checks

### `oss` — OSS-approved license

`build_licenses` resolves one SPDX string per repo — a manual `license`
assertion from `overrides.csv` first (highest priority, for repos whose
LICENSE detection fails upstream), then the registry license (each
ecosystem's `results.csv` `license` column, most common value across the
repo's packages, ties alphabetical), the GitHub API license as
fallback (with the GitLab project license as the equivalent fallback for
GitLab-hosted repos) — and classifies it against the OSS-approved set in
`data/sources/osi/oss-licenses.csv`. A source only claims the slot with a
**meaningful** license: an unknown sentinel (`noassertion` / `other` /
`none`) never shadows a real detection from a later source.

The approved set is derived by `src.sources.osi.fetch_licenses` from the raw
SPDX License List (`data/sources/spdx/licenses.csv`, fetched by
`src.sources.spdx.fetch_licenses` — the Linux Foundation registry carrying
both approval flags):

```
approved = isOsiApproved ∪ (isFsfLibre − content licenses) ∪ curated extras
```

FSF-libre adds genuine software licenses OSI never reviewed; its **content**
licenses (CC-BY-*, CC0, GFDL) are excluded — free for documents, not software
OSS. The curated extras are the handful neither body reviewed (curl, blessing,
…). SPDX expressions (`mit or apache-2.0`, `mit/apache-2.0`, `apache-2.0 with
llvm-exception`, `gpl-3.0-or-later`) count as OSS when ANY component is
approved.

In `licenses.csv` the flag is ternary — `True` / `False` / empty (no
license signal at all: empty, `noassertion`, `other`, `none`) — so unknown
stays distinguishable from known-non-OSS. The rollup treats only
`oss=True` as eligible (unknown counts as not-OSS there).

### `intent` and `nonprofit` — funding signals

Built by `build_funding` (full methodology, score formula and worked examples in
[components/funding.md](components/funding.md)). `intent` is True when the
repo shows at least one funding signal: the owner's GitHub Sponsors
listing being enabled (any inbound sponsor count implies it; outbound
sponsoring feeds only the score, NOT intent), a declared channel
(FUNDING.yml — a resolved funding link or the file's mere presence,
funding.json — repo- or org-level, npm/PyPI funding field, a real Open
Collective, a curated PayPal.me handle), a bus-factor maintainer with a
personal Sponsors listing (union of the fetched listings and the curated
`maintainer-overrides.csv`), or an institutional host/owner. `intent` then
propagates at the owner level: once any repo an owner has in scope
self-declares a channel, every repo of that owner gets `intent=True`
(GitHub's own org-`.github`/personal-Sponsors semantics — see
[components/funding.md](components/funding.md)). `nonprofit` defaults True and flips False only
when a curated/scraped `company` host or owner backs the repo — those
repos are already resourced, so they are ineligible but stay visible in
the table.

### `active` — not EOL, not archived

`build_active` combines two signals:

- **`eol`** — the manual end-of-life column of
  `data/eligibility/overrides.csv` (`True` = project declared end-of-life;
  an explicit `False` pins a repo alive; empty = no verdict → alive). The
  per-ecosystem registry EOL signals (`data/sources/<eco>/eol.csv`, from
  `src.sources.<eco>.check_eol`) flag *packages* (deprecations, yanks,
  endoflife.date dates) and inform that per-repo manual call.
- **`archived`** — the GitHub `archived` flag from
  `data/sources/github/repos.csv`. There is no exemption: an archived repo
  is inactive, full stop. A project whose canonical upstream lives off
  GitHub is repointed at that upstream in `data/value/overrides.csv` (a
  `git_url`-only row, blank `repo` slug) so it enters the pipeline as the
  live upstream rather than as the archived GitHub mirror — e.g. glibc
  resolves to its Debian salsa GitLab repo and pixman to
  gitlab.freedesktop.org, not to `bminor/glibc` / `libpixman/pixman`.

`active = NOT eol AND NOT archived`.

## Stage overrides — `data/eligibility/overrides.csv`

One curated row per repo — or per org, when `repo` is an `owner/*` glob
(applies to every in-scope repo under that owner that has no per-repo row;
a per-repo row always wins) — shared by three builders:

| Column | Used by | Meaning |
|---|---|---|
| `repo` | all | lowercased `owner/name` key (or `owner/*` org glob) |
| `repo_id` | all | stable GitHub id — the actual join key (rename-proof) |
| `host`, `host_type` | build_funding | legally-stewarding foundation/company (domain + company/nonprofit) |
| `gh_user` | — | GitHub login (informational) |
| `owner`, `owner_type` | build_funding | entity owning the GitHub org |
| `oc_slug` | build_funding | curated Open Collective slug; empty on a curated row = authoritative "no OC" |
| `license` | build_licenses | manual SPDX assertion (highest priority) when upstream detection fails — GitHub Licensee returns `noassertion` on bundled/dual/stacked LICENSE files, or the repo ships no standard LICENSE (e.g. node=`mit`, cpython=`python-2.0`, linux=`gpl-2.0-only`, icu=`unicode-3.0`) |
| `paypal` | build_funding | curated PayPal.me URL — a declared (unmeasured) funding channel |
| `eol` | build_active | manual end-of-life verdict (True/False/empty) |
| `reason` | — | free-text audit context for the override |

(Repo-identity/validity corrections stay in `data/value/overrides.csv` —
this file is only for eligibility-stage judgments.)

Two more curated files live in `data/eligibility/`:

| File | Used by | Meaning |
|---|---|---|
| `maintainer-overrides.csv` | build_funding | `login,reason` — bus-factor maintainers fundable through a channel the Sponsors fetch can't see; unioned into the fundable-maintainer set |
| `gitlab-hosts.csv` | build_funding | `gitlab_host,host,host_type,reason` — a repo on an institution's OWN GitLab (salsa.debian.org → debian.org, invent.kde.org → kde.org, …) is host-backed by that institution. `gitlab.com` is deliberately absent: commercial shared hosting backs nothing |

## Outputs

- `licenses.csv` — `repo, repo_id, license, license_source (override/registry/github/gitlab/""), oss`
- `active.csv` — `repo, repo_id, eol, archived, active`
- `funding.csv` — funding signals + score per repo
  (schema in [components/funding.md](components/funding.md))
- `eligibility.csv` — `repo, repo_id, oss, intent, nonprofit, active, eligible,
  value_comps, risk_comps, complete`

## Running

`scripts/run-pipeline.sh` is the only supported entry point — it runs
value → risk → eligibility → preview → health in order, so the preview
workbook and the health check never go stale behind a stage rebuild:

```bash
scripts/run-pipeline.sh --stage eligibility            # this stage alone
scripts/run-pipeline.sh --from-stage eligibility       # eligibility → preview → health
scripts/run-pipeline.sh --stage eligibility --list     # show this stage's steps
scripts/run-pipeline.sh --stage eligibility --skip-fetch   # build only, no fetchers
```

Per-step flags pass through to the stage runner
(`src.eligibility.run_eligibility_pipeline`), so `--list`, `--from <step>`
and `--skip-fetch` compose with stage selection.

Fetchers (all incremental / TTL-gated): the GitHub repo-owner refresh
(archived flag + license fallback), the GitLab project refresh (GitLab
license fallback), the SPDX license list + the derived OSS-approved set,
the per-eco license and EOL fetchers, the bus-factor contributor-commit
fetch (`bf-contributors`, serial, ahead of the funding group), the
funding-intent fetchers (FUNDING.yml, the GitLab funding-file probe,
npm/PyPI funding, inbound/outbound Sponsors, bus-factor maintainer Sponsors,
FLOSS Fund, the Open Collective reverse-map + budgets) and the
FOSS-foundation roster scrapers + host matcher. Builders:
`licenses` → `active` → `funding-build` → `aggregate`. The `data/preview/`
deliverables (`results`, `data`, `people`, `preview-xlsx`) live in their own
runner — `src.run_preview_pipeline`, the stage after this one — since they roll
up all three stages, not just eligibility.

`scripts/pipeline_health.py` (the `health` stage) verifies every stage CSV
matches its builder's current output; `scripts/stats.py` recomputes the
preview pipeline sheet coverage tables.

## The bus-factor maintainer cache

`bf_maintainer_fundable` (an `intent` signal) is keyed off each repo's
bus-factor set, read from `data/sources/github/contributor-commits.csv`. That
file is written by the `bf-contributors` step
(`src.sources.github.fetch_contributors_metrics`, 90-day per-row TTL), which
runs serially, before the `funding` pgroup — both `maintainer-sponsors` (which
picks which logins to query from it) and `funding-build` read its output, so
every repo in scope has bus-factor rows before either consumer runs. The
git-clone contributor log (`data/sources/git/contributor-commits.csv`) that
feeds the risk stage's concentration score is a *different* file, fetched
independently.
