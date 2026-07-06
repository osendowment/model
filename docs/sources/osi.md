# OSI (OSS License List)

The set of SPDX license ids the model treats as software open source:
SPDX `isOsiApproved` ∪ a hand-curated `EXTRAS` dict. Feeds the eligibility
stage's `oss` flag — a repo's resolved license (or any component of its SPDX
expression) must be in this set; see the license section of
[eligibility.md](../eligibility.md), counts in
[stats.md](../stats.md#licenses-scope-940).

Strict software OSS, **not** FSF "free software" — FSF's `isFsfLibre` also
covers content licenses (CC-BY-4.0, CC0-1.0, GFDL) that are free for
documents/data but not software OSS, so they are deliberately excluded.

## Data Source

**Source**: the [SPDX license-list-data](https://github.com/spdx/license-list-data)
JSON dump (`json/licenses.json`, raw from GitHub) — **not** the OSI API. SPDX
ids match what npm/PyPI/crates/Homebrew publishers actually declare, the OSI's
own JSON API returns empty bodies, and SPDX carries both the `isOsiApproved`
flag and a `seeAlso` list that usually includes the OSI license-page URL.
No authentication (single JSON download).

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
| `source` | `osi` / `extras` | which inclusion rule admitted it |
| `is_deprecated` | `False` | SPDX deprecated the id (still OSS) |
| `reference` | `https://spdx.org/licenses/Apache-2.0.html` | SPDX page |
| `osi_url` | `https://opensource.org/license/apache-2.0` | first opensource.org link in `seeAlso`; empty for most extras |
| `fetched_at` | `2026-05-03T22:48:48Z` | UTC timestamp, one per fetch run |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/osi/fetch_licenses.py` | Fetch SPDX list, apply `isOsiApproved` ∪ `EXTRAS`, write CSV |

```bash
uv run python -m src.sources.osi.fetch_licenses          # respects 90-day TTL
uv run python -m src.sources.osi.fetch_licenses --force  # ignore TTL
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
