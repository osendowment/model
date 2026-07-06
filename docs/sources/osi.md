# OSS License Set (unified OSI ∪ FSF)

The set of SPDX license ids the model treats as software open source:
`isOsiApproved` ∪ (`isFsfLibre` − content licenses) ∪ a hand-curated
`EXTRAS` dict. Feeds the eligibility stage's `oss` flag — a repo's resolved
license (or any component of its SPDX expression) must be in this set; see
the license section of [eligibility.md](../eligibility.md), counts in
[stats.md](../stats.md#licenses-scope-940).

Two review bodies are unified: OSI's formal approvals and the FSF's
free-software list (as carried by SPDX's `isFsfLibre` flag). FSF-libre
CONTENT licenses (CC-BY-*, CC0, GFDL, OFL fonts, ODbL data) are free for
documents/data but not software OSS, so they are deliberately excluded
(`CONTENT_LICENSE_PREFIXES` in `src/sources/osi/fetch_licenses.py`). Running
the builder prints the OSI-vs-FSF comparison (`--compare` for report-only).

## Data Source

**Source**: `data/sources/spdx/licenses.csv` — the full SPDX License List
stored by `src.sources.spdx.fetch_licenses` (see [spdx.md](spdx.md)); this
module takes no network of its own. SPDX ids match what
npm/PyPI/crates/Homebrew publishers actually declare, and the list carries
both approval flags per license.

## Curated extras

`EXTRAS` in `src/sources/osi/fetch_licenses.py` is the source of truth for the
curated list — licenses universally treated as software OSS that OSI never
formally reviewed (usually nobody filed the paperwork). Each entry carries an
evidence comment in the script (project, why OSI omits it, source URLs); new
entries loosen eligibility, so they require comparable evidence. There is no
separate extras data file — the fetcher merges both passes into one CSV and
the `source` column records which rule admitted each row.

| `spdx_id` | Project | License family |
|-----------|---------|----------------|
| `blessing` | SQLite | public-domain dedication |
| `curl` | curl | MIT/X11 derivative |
| `ftl` | FreeType | BSD-style attribution |
| `libpng-2.0` | libpng | zlib/MIT-style permissive |
| `mit-cmu` | Pillow | MIT variant (CMU) |
| `psf-2.0` | CPython / PSF tooling | ≡ OSI-approved `python-2.0` |
| `x11-distribute-modifications-variant` | ncurses | MIT/X11-family permissive |

## Raw Data

`data/sources/osi/oss-licenses.csv` — one row per admitted SPDX id:

| Column | Example | Description |
|--------|---------|-------------|
| `spdx_id` | `apache-2.0` | lowercased SPDX id — the join key |
| `spdx_id_canonical` | `Apache-2.0` | original SPDX casing |
| `name` | `Apache License 2.0` | full human-readable name |
| `source` | `osi` / `fsf` / `extras` | which inclusion rule admitted it (`osi` wins the label when both bodies list it) |
| `is_deprecated` | `False` | SPDX deprecated the id (still OSS) |
| `reference` | `https://spdx.org/licenses/Apache-2.0.html` | SPDX page |
| `osi_url` | `https://opensource.org/license/apache-2.0` | first opensource.org link in `seeAlso`; empty for most extras |
| `fetched_at` | `2026-05-03T22:48:48Z` | UTC timestamp, one per fetch run |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/spdx/fetch_licenses.py` | Fetch the full SPDX License List (both flags) → `data/sources/spdx/licenses.csv` |
| `src/sources/osi/fetch_licenses.py` | Build the unified OSS set from it (`osi` ∪ `fsf` software ∪ `EXTRAS`), print the OSI-vs-FSF comparison, write CSV |

```bash
uv run python -m src.sources.osi.fetch_licenses            # respects 90-day TTL
uv run python -m src.sources.osi.fetch_licenses --force    # refetch SPDX + rebuild
uv run python -m src.sources.osi.fetch_licenses --compare  # comparison only
```

## Refresh & Caveats

| Aspect | Behavior |
|--------|----------|
| TTL | 90 days — re-runs within the window are no-ops unless `--force` |
| Self-bootstrap | `ensure()` runs from `build_licenses.load_oss_approved` and as the `osi` fetch step of `run_eligibility_pipeline` — the CSV regenerates when missing/stale |
| Audit trail | each fetch rewrites the whole file with one new `fetched_at`; `source` says which rule admitted each row |
| Extras → osi migration | if OSI approves an extra and SPDX flips `isOsiApproved`, the next refetch moves it to `source=osi` automatically |
| Deprecated ids | stay in the set (`is_deprecated=True`) — projects still declare deprecated ids; the eligibility test is membership, not currency |
| Schema contract | keyed by SPDX id, not repo — exempt from the repo-keyed `repo_id` contract |
