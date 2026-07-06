# Eligibility Pipeline

Stage 3 of the pipeline (Value → Risk → **Eligibility**). Takes the top
repos and answers one question per repo: **is it eligible for funding?**
Five independent checks, one AND-rollup:

```
eligible = oss AND intent AND nonprofit AND active AND code
```

| Check | Meaning | Built by | Output |
|---|---|---|---|
| `oss` | OSI-approved license | `src.eligibility.build_licenses` | `data/eligibility/licenses.csv` |
| `intent` | shows any funding signal | `src.eligibility.build_funding` | `data/eligibility/funding.csv` |
| `nonprofit` | not company-backed | `src.eligibility.build_funding` | `data/eligibility/funding.csv` |
| `active` | not EOL, not archived | `src.eligibility.build_active` | `data/eligibility/active.csv` |
| `code` | measured snapshot has code (loc > 0) | `src.eligibility.build_eligibility` (from `data/risk/complexity.csv` `loc_eoy`) | — |

`src.eligibility.build_eligibility` joins the five flags into
`data/eligibility/eligibility.csv`. Coverage counts live in
[stats.md](stats.md#eligibility).

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
(`risk_input.value_classes = ["A"]`), loaded via `load_top_repos()`, which
now **includes archived repos by default** (every stage shares this scope).
Archived repos must appear in the stage output as `active=False` so the reason
for their ineligibility is visible, rather than being silently dropped before
the stage runs.

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
│   ├── osi approved set            ← data/sources/osi/oss-licenses.csv
│   │                                 (SPDX isOsiApproved ∪ curated extras,
│   │                                  90-day TTL, self-bootstrapping)
│   └── oss                         ← ternary True / False / "" (unknown);
│                                     SPDX-expression-aware
│
├── intent + nonprofit — Funding (→ funding.csv, see components/funding.md)
│   ├── GitHub Sponsors in/out, FUNDING.yml, funding.json, npm/PyPI
│   │   funding fields, bus-factor-maintainer Sponsors, Open Collective budgets
│   ├── FOSS-foundation hosts       ← data/sources/funding/host-by-repo.csv
│   └── curated host/owner backing  ← data/eligibility/overrides.csv
│
├── active — Activity (→ active.csv)
│   ├── eol                         ← data/eligibility/overrides.csv `eol`
│   │                                 (manual per-repo verdict; the per-eco
│   │                                  check_eol registry signals are its
│   │                                  advisory inputs)
│   ├── archived                    ← data/sources/github/repos.csv
│   └── mirror exemption            ← live-upstream GitHub mirrors
│                                     (data/value/overrides.csv rows with a
│                                      non-github git_url, e.g. bminor/glibc)
│
├── code — Has source code (derived in build_eligibility)
│   └── loc_eoy > 0                 ← data/risk/complexity.csv (scc snapshot)
│
└── Final rollup (→ eligibility.csv)
    └── eligible = oss AND intent AND nonprofit AND active AND code
```

## The five checks

### `oss` — OSI-approved license

`build_licenses` resolves one SPDX string per repo — a manual `license`
assertion from `overrides.csv` first (highest priority, for repos whose
LICENSE detection fails upstream), then the registry license (each
ecosystem's `results.csv` `license` column, most common value across the
repo's packages, ties alphabetical), the GitHub API license as
fallback — and classifies it against the OSS-approved set in
`data/sources/osi/oss-licenses.csv` (SPDX `isOsiApproved` ∪ a small curated
extras list — curl, ftl, libpng-2.0, mit-cmu, psf-2.0, blessing; content
licenses like CC-BY/CC0 are deliberately **not** OSS). SPDX expressions
(`mit or apache-2.0`, `mit/apache-2.0`, `apache-2.0 with llvm-exception`,
`gpl-3.0-or-later`) count as OSS when ANY component is approved.

In `licenses.csv` the flag is ternary — `True` / `False` / empty (no
license signal at all: empty, `noassertion`, `other`, `none`) — so unknown
stays distinguishable from known-non-OSS. The rollup treats only
`oss=True` as eligible.

### `intent` and `nonprofit` — funding signals

Built by `build_funding` (moved unchanged from the risk stage — full
methodology, score formula and worked examples in
[components/funding.md](components/funding.md)). `intent` is True when the
repo shows at least one funding signal: a GitHub sponsorship (in or out),
the owner's Sponsors listing, a declared channel (FUNDING.yml,
funding.json, npm/PyPI funding field, a real Open Collective, a curated
PayPal.me handle), a bus-factor maintainer with a personal Sponsors
listing, or an institutional host/owner. `nonprofit` defaults True and flips False only
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
  `data/sources/github/repos.csv`, EXCEPT live-upstream mirrors (the
  archived flag sits on the GitHub mirror while the real upstream — e.g.
  sourceware.org for `bminor/glibc` — is alive; same exemption set
  `load_top_repos` uses, exposed as `repos.load_live_upstream_mirrors`).

`active = NOT eol AND (NOT archived OR mirror)`.

### `code` — the repo has source code

Derived directly in `build_eligibility` from the risk stage's complexity
snapshot (`data/risk/complexity.csv` `loc_eoy`, the scc measurement of the
repo's most recent code-bearing end-of-year tree): `code = loc_eoy > 0`.
There is no per-dimension CSV — the auditable detail (`loc_eoy`, `loc_year`)
already lives in `complexity.csv`.

`False` means scc measured zero source lines at every snapshot — an archived
stub whose default branch was stripped to a README (e.g.
`bincode-org/bincode`, `isaacs/inflight-deprecated-do-not-use`). There is no
code to fund, so the repo is ineligible regardless of its other flags. A
missing measurement also reads `False`, but complexity covers 100% of the
scope so in practice this only ever reflects a measured zero. Note the check
is per-snapshot, not per-current-tree: a repo whose code was removed *after*
its last measured snapshot (e.g. `googleapis/python-cloud-core`, emptied in
2026 after a code-bearing 2025 snapshot) stays `code=True` until a newer
snapshot is measured.

## Stage overrides — `data/eligibility/overrides.csv`

One curated row per repo, shared by two builders:

| Column | Used by | Meaning |
|---|---|---|
| `repo` | both | lowercased `owner/name` key |
| `host`, `host_type` | build_funding | legally-stewarding foundation/company (domain + company/nonprofit) |
| `gh_user` | — | GitHub login (informational) |
| `owner`, `owner_type` | build_funding | entity owning the GitHub org |
| `oc_slug` | build_funding | curated Open Collective slug; empty on a curated row = authoritative "no OC" |
| `license` | build_licenses | manual SPDX assertion (highest priority) when upstream detection fails — GitHub Licensee returns `noassertion` on bundled/dual/stacked LICENSE files, or the repo ships no standard LICENSE (e.g. node=`mit`, cpython=`python-2.0`, linux=`gpl-2.0-only`, icu=`unicode-3.0`) |
| `eol` | build_active | manual end-of-life verdict (True/False/empty) |
| `reason` | — | free-text audit context for the override |

(Repo-identity/validity corrections stay in `data/value/overrides.csv` —
this file is only for eligibility-stage judgments.)

## Outputs

- `licenses.csv` — `repo, repo_id, license, license_source (override/registry/github/""), oss`
- `active.csv` — `repo, repo_id, eol, archived, mirror, active`
- `funding.csv` — funding signals + score per repo
  (schema in [components/funding.md](components/funding.md))
- `eligibility.csv` — `repo, repo_id, oss, intent, nonprofit, active, code, eligible, value_comps, risk_comps, complete`

## Running

```
uv run python -m src.eligibility.run_eligibility_pipeline                # fetch + build
uv run python -m src.eligibility.run_eligibility_pipeline --skip-fetch   # build only
uv run python -m src.eligibility.run_eligibility_pipeline --list         # show steps
```

Fetchers (all incremental / TTL-gated): the GitHub repo-owner refresh
(archived flag + license fallback), the OSI license list, the per-eco
license and EOL fetchers, the funding-intent fetchers (FUNDING.yml,
npm/PyPI funding, Sponsors, FLOSS Fund, Open Collective) and the
FOSS-foundation roster scrapers + host matcher. Builders: `licenses` →
`active` → `funding-build` → `aggregate`.

`scripts/pipeline_health.py` verifies every stage CSV matches its
builder's current output; `scripts/stats.py` recomputes the
[stats.md](stats.md#eligibility) coverage tables.
