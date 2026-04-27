# Eligibility Pipeline

Determines which GitHub repos qualify for funding. Two checks: open-source
license status, and EOL (end-of-life) status.

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

    npm --> npm_eol --> unify["unify_value_data.py"]
    pypi --> pypi_eol --> unify
    crates --> crates_eol --> unify
    cpp --> cpp_eol --> unify

    unify --> value["value-data.csv<br/>(adds is_eol col)"]

    subgraph Eligibility["Eligibility"]
        license["OSS License Check"]
        eol_join["Per-repo EOL aggregation"]
    end

    github --> license
    value --> eol_join
    license --> output["eligibility-data.csv"]
    eol_join --> output
```

## How It Works

### License check

Classifies each repo's license against the OSI-approved license list using
metadata from `data/github/search/top-repos.csv`. 63 licenses are recognized,
including MIT, Apache 2.0, GPL (all versions), BSD variants, MPL, ISC,
Unlicense, and others.

### EOL check

EOL is determined per-ecosystem at the **package** level using
maintainer-set, registry-level signals — not GitHub's `archived` flag, which
is unreliable for projects whose canonical repo lives elsewhere (glibc,
Apache, lots of mirrors).

Each ecosystem has its own `check_eol.py` that writes
`data/{ecosystem}/eol.csv`. `unify_value_data.py` then aggregates a single
`is_eol` column into `data/value-data.csv`. `eligibility.py` derives
**per-repo** EOL from the package-level signal: a repo is EOL only if every
package it produces is EOL (handles monorepos with mixed states).

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
php, etc.). A product is EOL only if every release cycle's `eol` date is in
the past. A cycle with `eol: false` keeps the project alive.

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
| `src/unify_value_data.py` | Join per-eco eol → `is_eol` col in `data/value-data.csv` | `uv run python -m src.unify_value_data` |
| `src/eligibility.py` | Final eligibility per repo → `data/eligibility-data.csv` | `uv run python -m src.eligibility` |

Run order: each ecosystem's `check_eol.py` → `unify_value_data.py` → `eligibility.py`.

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

One row per (package, ecosystem). Adds `is_eol` joined from per-ecosystem `eol.csv`.

| Column | Description |
|--------|-------------|
| `package`, `ecosystem`, `github_repo`, `pagerank`, `value_class` | (see `docs/value.md`) |
| `is_eol` | `True` if the package's `is_eol` in `data/{eco}/eol.csv` is `True`; defaults to `False` if no row found |

### data/eligibility-data.csv

Final per-repo eligibility table. `eligibility = is_oss AND NOT is_eol`.

| Column | Description |
|--------|-------------|
| `repo` | GitHub repo slug (`owner/name`) |
| `repo_id` | GitHub numeric repo ID |
| `user` | Repo owner login |
| `user_id` | Owner numeric ID |
| `user_type` | `User` or `Organization` |
| `license` | License SPDX key |
| `is_oss` | `True` if the license is OSI-approved |
| `is_eol` | `True` if every package mapped to this repo (in `value-data.csv`) is EOL. Repos with no packages in `value-data.csv` default to `False`. |
| `tm_owner` | Trademark owner (TODO) |
| `tm_owner_type` | Corporate vs community-held (TODO) |
| `eligibility` | `True` if `is_oss AND NOT is_eol` |
