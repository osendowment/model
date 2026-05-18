# Eligibility Pipeline

Determines which GitHub repos qualify for funding. Two checks: open-source
license status, and EOL (end-of-life) status.

## Metrics Roadmap

Target shape of inputs per dimension. Each leaf = one metric, with its data
source and the time period it represents. Per-ecosystem rows feed the
GitHub rollup that becomes `eligibility-data.csv`.

> **Note:** `[most recent]` means the latest available pull of that source.
> Eligibility is a *current-state* check — it has no historical window.

```
Eligibility
│
├── Scope gate
│   └── value_class ∈ {A, B}      ← data/value-data.csv                 [2021–2025]
│                                    (C/D dropped before any check)
│
├── License (OSS check)
│   ├── package_license           ← npm:    registry.npmjs.org          [most recent]
│   │                                pypi:   pypi.org/pypi/<n>/json     [most recent]
│   │                                crates: crates.io DB-dump          [most recent]
│   │                                cpp:    Homebrew formula.json      [most recent]
│   ├── repo_license (fallback)   ← GitHub Licensee detection           [most recent]
│   ├── osi_approved_set          ← SPDX list filtered isOsiApproved=T  [most recent]
│   │                                (90-day TTL → data/osi/oss-licenses.csv)
│   └── is_oss                    ← derived ternary: True / False / "" [most recent]
│                                    SPDX-expression-aware vs OSI set
│
├── EOL (per-package signals → AND-aggregate per repo)
│   ├── npm_deprecated            ← npm registry `deprecated` field    [most recent]
│   ├── pypi_inactive             ← Trove "Development Status :: 7"    [most recent]
│   ├── crates_yanked             ← crates.io DB-dump default-ver yank [most recent]
│   ├── homebrew_disabled         ← formulae.brew.sh formula.json      [most recent]
│   ├── homebrew_deprecated       ← formulae.brew.sh formula.json      [most recent]
│   ├── endoflife_date (overlay)  ← endoflife.date/api/<product>.json  [most recent]
│   │                                whitelist of ~20 well-known products
│   └── is_eol                    ← derived (every constituent pkg EOL)[most recent]
│
├── Repo state (GitHub API)
│   ├── valid_repo                ← /repos/{o}/{r} HTTP 200 vs 404     [most recent]
│   └── repo_url                  ← /repos homepage                    [most recent]
│
├── Owner / governance
│   ├── user / user_id / user_type
│   │                              ← /repos owner_login + users.csv    [most recent]
│   ├── repo_owner / repo_owner_url
│   │                              ← data/github/users.csv             [most recent]
│   └── host                      ← data/foundations/host-by-repo.csv  [most recent]
│                                    (apache/cncf/eclipse/openjs/
│                                     psf/lf/numfocus/sfc)
│
└── Final rollup (→ eligibility-data.csv)
    └── eligibility               ← valid_repo                         [most recent]
                                     AND is_oss is True
                                     AND NOT is_eol
```

```mermaid
graph LR
    github["GitHub"]
    npm["npm registry"]
    pypi["PyPI"]
    crates["crates.io DB dump"]
    cpp["—"]

    subgraph EOL["Per-ecosystem EOL"]
        npm_eol["npm/check_eol.py<br/>npm_deprecated"]
        pypi_eol["pypi/check_eol.py<br/>pypi_inactive"]
        crates_eol["crates/check_eol.py<br/>crates_yanked"]
        cpp_eol["cpp/check_eol.py<br/>unsupported"]
    end

    npm --> npm_eol --> unify["src.pipeline.run_value_pipeline"]
    pypi --> pypi_eol --> unify
    crates --> crates_eol --> unify
    cpp --> cpp_eol --> unify

    unify --> value["value-data.csv<br/>(per-repo, no is_eol)"]

    subgraph Eligibility["Eligibility"]
        license["OSS License Check"]
        eol_join["Per-repo EOL join<br/>(per-eco eol.csv ⨝ results.csv)"]
    end

    github --> license
    npm_eol --> eol_join
    pypi_eol --> eol_join
    crates_eol --> eol_join
    cpp_eol --> eol_join
    license --> output["eligibility-data.csv"]
    eol_join --> output
```

## How It Works

> **Pipeline order**: Eligibility now runs **after** the Risk stage
> (`Value → Risk → Eligibility`). Its intended scope (future work, not yet
> coded) is repos with `value_class=A` that also have the highest risk
> class. For now the code is unchanged — it still filters to all AB-class
> repos — but it is no longer the Risk pipeline's input.

### Source of truth and scope

Eligibility reads exclusively from `data/github/repos.csv` (populated by
`src.github.fetch_repo_owner_data`). No fallback to discovery data: a repo
must have a fresh GitHub API record to appear in eligibility at all.

**Scope is AB-class only.** A repo must satisfy all three:

1. Be in `value-data.csv` with `class ∈ {A, B}` (`ELIGIBLE_CLASSES` in
   `eligibility.py` — adjust there if scope changes).
2. Be in `data/github/repos.csv` (we have a fresh GitHub API record).
3. Pass the OSS license + EOL checks below.

C/D-class repos are dropped before any other check — they're tracked in
the value pipeline but not funding-eligible.

Repos that returned HTTP 404 are recorded with `valid=False` in
`repos.csv` and surface in eligibility as `valid_repo=False`,
`is_oss=""` (unknown — no license to inspect), `eligibility=False`.

### License check

Classifies each repo's `license` (from the GitHub API) against the OSI-approved
license list. 63 licenses are recognized, including MIT, Apache 2.0, GPL (all
versions), BSD variants, MPL, ISC, Unlicense, and others.

### EOL check

EOL is determined per-ecosystem at the **package** level using
maintainer-set, registry-level signals — not GitHub's `archived` flag, which
is unreliable for projects whose canonical repo lives elsewhere (glibc,
Apache, lots of mirrors).

Each ecosystem has its own `check_eol.py` that writes
`data/{ecosystem}/eol.csv`. `eligibility.py` joins each per-eco
`eol.csv` directly with the matching `data/{eco}/results.csv` (for the
`package → github_repo` map) and aggregates: a repo is `is_eol=True`
iff **every** constituent package across all 4 ecosystems is
`is_eol=True` (handles monorepos and cross-ecosystem polyglot projects).
`value-data.csv` deliberately does **not** carry `is_eol` — it's an
eligibility concern, not a value-pipeline one.

| Ecosystem | Signal | `eol_method` | Source |
|---|---|---|---|
| **npm** | latest version's `deprecated` field on the registry | `npm_deprecated` | `registry.npmjs.org` |
| **pypi** | `Development Status :: 7 - Inactive` Trove classifier | `pypi_inactive` | `pypi.org/pypi/<n>/json` |
| **crates** | default version is `yanked` | `crates_yanked` | local crates.io DB dump |
| **cpp** | every Homebrew formula for the project is `disabled` or `deprecated` | `homebrew_disabled` / `homebrew_deprecated` | `formulae.brew.sh/api/formula.json` (one bulk fetch) |
| **cpp** (overlay) | every release cycle's `eol` date is in the past | `endoflife_date` | `endoflife.date/api/<product>.json` (curated whitelist of ~20 well-known products) |

`crates_yanked` has low recall — `cargo yank` is meant for buggy versions,
not deprecation. crates.io has no formal "deprecate" mechanism; the column
is honest about that.

### cpp signal details

A cpp project has at most one Homebrew "EOL" classification: it's only
flagged if **every** Homebrew formula mapped to that project (via
Repology's `repo='homebrew'` rows) is `disabled` or `deprecated`. This
correctly handles versioned formulas — `gcc` has formulas for `gcc`, `gcc@9`,
`gcc@10` etc.; the old version-pinned ones being deprecated doesn't make
gcc itself EOL.

`endoflife_date` is an overlay applied on top of the Homebrew check for a
small whitelist of well-known products (openssl, postgresql, python, ruby,
php, etc.). We model **project-level** EOL, not version-level: a project is
EOL iff `max(eol date across all cycles) < today`. A cycle with
`eol: false` (vendor-declared open-ended support) keeps the project alive
regardless of any past-EOL cycles.

Examples (today = 2026-04-27):

| Product | `max(eol)` | Result |
|---|---|---|
| angularjs | 2021-12-31 | ✅ EOL |
| centos | 2024-06-30 | ✅ EOL |
| openssl | 2030-04-08 (cycle 3.5) | alive |
| python | 2030-10-31 (cycle 3.14) | alive |
| internet-explorer | 2031-10-14 (cycle 11) | alive (MS extended Win10 lifecycle support) |
| redis | one cycle has `eol: false` | alive |

### Why not Debian "removed from current stable"?

Considered and rejected — high false-positive rate. A package can be absent
from current Debian stable for many reasons unrelated to EOL:

- **SONAME bumps** (`libpng12-0` removed; `libpng16-16` is current and alive)
- **python2→3 transitions** (`python-six` removed; `python3-six` alive)
- **Source-package renames** (`nodejs-legacy` folded into `nodejs`)
- **Held during release transitions** (in unstable awaiting unblock)
- **RC-bug or FTBFS removals** — alive upstream, transient Debian state
- **Section reorgs** (non-free / contrib moves)
- **Architecture-specific removals** (only dropped for `armhf` etc.)
- **Hosted entirely outside Debian** (many GNU/sourceware projects)

A cleaner Debian signal would parse `ftp-master.debian.org/removals.txt`
and filter to entries with `Reason:` containing `RoQA`, `Dead upstream`,
`Orphaned and abandoned upstream`, or similar — that's an explicit Debian
FTP-team statement of upstream EOL with very low FP rate. Deferred for now
since it requires parsing an unstructured log.

## Scripts

| Script | Purpose | Command |
|--------|---------|---------|
| `src/{eco}/check_eol.py` | Flag EOL packages → `data/{eco}/eol.csv` | `uv run python -m src.npm.check_eol` |
| `src/{eco}/fetch_licenses.py` | Add `license` (lowercase SPDX) to `data/{eco}/results.csv` from each registry (npm/PyPI live API; crates DB dump; Homebrew raw cache; cpp joined from Homebrew) | `uv run python -m src.npm.fetch_licenses` |
| `src/osi/fetch_licenses.py` | Refresh the OSI-approved SPDX list (90-day TTL) → `data/osi/oss-licenses.csv`. Sourced from the SPDX license list filtered by `isOsiApproved=true`. | `uv run python -m src.osi.fetch_licenses` |
| `src/github/fetch_repo_owner_data.py` | Authoritative repo + owner data → `data/github/{repos,users}.csv` | `uv run python -m src.github.fetch_repo_owner_data` |
| `src/foundations/match_repos.py` | Determine FOSS-foundation host per repo → `data/foundations/host-by-repo.csv` | `uv run python -m src.foundations.match_repos` |
| `src/pipeline/value/unify_value_data.py` | Unify per-eco results into `data/value-data.csv` | `uv run python -m src.pipeline.run_value_pipeline` |
| `src/pipeline/eligibility/classify_eligibility.py` | Final eligibility per repo → `data/eligibility-data.csv` | `uv run python -m src.pipeline.run_eligibility_pipeline` |

Run order:
1. per-ecosystem `check_eol.py` and `fetch_licenses.py` (parallelisable)
2. `src.osi.fetch_licenses` (refreshes the OSI list — TTL'd, usually a no-op)
3. `src.pipeline.run_value_pipeline` (unifies per-eco results → `value-data.csv`)
4. `src.pipeline.run_risk_pipeline` (scores A/B repos → `risk-data.csv`)
5. `src.github.fetch_repo_owner_data` (populates the repo-level source of truth)
6. `src.foundations.match_repos` (host classification)
7. `src.pipeline.run_eligibility_pipeline` (joins everything → `eligibility-data.csv`)

License priority inside `eligibility.py`:
1. **Per-eco `results.csv`** — registry-declared SPDX (most authoritative; the package author set it).
2. **Fallback: `data/github/repos.csv`** — GitHub API's Licensee detection.

`is_oss` is **ternary** with strict OSI semantics:

- **`True`** — license (or any token in an SPDX expression) is in `data/osi/oss-licenses.csv`. Handles `mit or apache-2.0`, `gpl-3.0-or-later`, `apache-2.0 with llvm-exception or mit`, etc.
- **`False`** — license is **known but not OSI-approved**: CC-BY, CC0, GFDL, WTFPL, MIT-CMU, proprietary EULAs, etc.
- **`""` (empty)** — license is **unknown**: GitHub returned `noassertion` and we have no per-eco registry data to disambiguate, or no license declared anywhere.

**Eligibility requires `is_oss=True`** — both `False` and `""` produce `eligibility=False`. We won't fund a repo we can't verify is OSS.

## Output

### data/{ecosystem}/eol.csv

Per-package EOL details. Same schema for every ecosystem.

| Column | Description |
|--------|-------------|
| `package` | Package name (matches `data/{eco}/results.csv`) |
| `is_eol` | `True` if the registry-level signal indicates EOL |
| `eol_method` | `npm_deprecated`, `pypi_inactive`, `crates_yanked`, or `unsupported` |
| `eol_reason` | Human-readable evidence (deprecation message, classifier name) |
| `source` | `registry`, `db-dump`, `not_found`, `error`, or `unsupported` |
| `eol_checked_at` | ISO 8601 UTC timestamp of when this row's EOL was checked |

### data/value-data.csv

One row per **GitHub repo** (or per orphan package without a github_repo).
See `docs/value.md` for the full schema. Eligibility uses it indirectly:
`src/eligibility.py` reads each ecosystem's `data/{eco}/eol.csv` joined to
`data/{eco}/results.csv` to compute per-repo EOL — `value-data.csv` itself
does **not** carry an `is_eol` column.

### data/eligibility-data.csv

Final per-repo eligibility table. `eligibility = valid_repo AND (is_oss is True) AND NOT is_eol`.
A `False` or unknown (`""`) `is_oss` both produce `eligibility=False`.
Sourced exclusively from `data/github/repos.csv` — no fallbacks.

| Column | Description |
|--------|-------------|
| `repo` | GitHub repo slug (`owner/name`) |
| `repo_id` | GitHub numeric repo ID (empty if `valid_repo=False`) |
| `valid_repo` | `True` if `/repos/{owner}/{repo}` returned 200; `False` if 404 (repo deleted, renamed, or never existed). Repos absent from `data/github/repos.csv` are absent from this table — there is no third state. |
| `user` | Repo owner login (from `repos.csv.owner_login`) |
| `user_id` | Owner numeric ID |
| `user_type` | `User` or `Organization` |
| `license` | License SPDX key (from the GitHub API) |
| `is_oss` | Ternary — `True` if OSI-approved (loaded from `data/osi/oss-licenses.csv`); `False` if the license is known but not OSI-approved (CC-BY, CC0, MIT-CMU, …); `""` (empty) if no usable license signal (GitHub `noassertion`, no per-eco registry data, or empty). |
| `is_eol` | `True` if every package mapped to this repo (joined via per-eco `data/{eco}/results.csv` ↔ `data/{eco}/eol.csv`) is `is_eol=True`. Repos with no constituent packages default to `False`. |
| `host` | Slug of FOSS foundation hosting the project: `apache`, `cncf`, `eclipse`, `openjs`, `psf`, `lf`, `numfocus`, `sfc`. Empty if not foundation-hosted. Joined from `data/foundations/host-by-repo.csv`. |
| `repo_url` | Repo's homepage URL from the GitHub API (empty if not set on the repo). |
| `repo_owner` | Owner display name from `data/github/users.csv` (e.g. "The Apache Software Foundation"). Empty if not in users.csv. |
| `repo_owner_url` | Owner's `blog` URL from `data/github/users.csv`. Empty if not set. |
| `repo_owner_type` | TODO — `company` / `nonprofit` / `individual` / `community` / `government` classification. |
| `tm_owner` | Trademark owner (TODO) |
| `tm_owner_type` | Corporate vs community-held (TODO) |
| `eligibility` | `True` only if `valid_repo AND is_oss is True AND NOT is_eol`. `is_oss=False` or `is_oss=""` both produce `eligibility=False`. |
