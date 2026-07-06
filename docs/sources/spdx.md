# SPDX License List (Linux Foundation)

The canonical machine-readable license registry, maintained by the
[SPDX project](https://spdx.dev/) under the Linux Foundation. Stored in
full — every SPDX id with BOTH approval flags — as the single upstream for
the model's unified OSS-approved set ([osi.md](osi.md), built by
`src.sources.osi.fetch_licenses`).

## Data Source

**Source**: the [spdx/license-list-data](https://github.com/spdx/license-list-data)
JSON dump (`json/licenses.json`, raw from GitHub). No authentication
(single JSON download). SPDX ids match what npm/PyPI/crates/Homebrew
publishers actually declare.

Per license, two independent review-body flags:

| Flag | Body | Meaning |
|------|------|---------|
| `isOsiApproved` | Open Source Initiative | passed OSI's formal license review |
| `isFsfLibre` | Free Software Foundation | listed as free on gnu.org/licenses/license-list (software AND content licenses) |

## Raw Data

`data/sources/spdx/licenses.csv` — one row per SPDX id (the whole list,
no filtering; policy lives in the OSS-set builder):

| Column | Example | Description |
|--------|---------|-------------|
| `spdx_id` | `apache-2.0` | lowercased SPDX id — the join key |
| `spdx_id_canonical` | `Apache-2.0` | original SPDX casing |
| `name` | `Apache License 2.0` | full human-readable name |
| `is_osi_approved` | `True` | SPDX `isOsiApproved` |
| `is_fsf_libre` | `True` | SPDX `isFsfLibre` (absent in the JSON → `False`) |
| `is_deprecated` | `False` | SPDX deprecated the id |
| `reference` | `https://spdx.org/licenses/Apache-2.0.html` | SPDX page |
| `osi_url` | `https://opensource.org/license/apache-2.0` | first opensource.org link in `seeAlso` |
| `fetched_at` | `2026-07-06T15:00:00Z` | UTC timestamp, one per fetch run |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/spdx/fetch_licenses.py` | Fetch the list → `licenses.csv` (90-day TTL, `--force` to refetch) |

```bash
uv run python -m src.sources.spdx.fetch_licenses
uv run python -m src.sources.spdx.fetch_licenses --force
```

## Refresh & Caveats

| Aspect | Behavior |
|--------|----------|
| TTL | 90 days — re-runs within the window are no-ops unless `--force` |
| Self-bootstrap | `ensure()` is invoked by the OSS-set builder and by the `spdx` fetch step of `run_eligibility_pipeline` |
| `isFsfLibre` coverage | SPDX only marks licenses the FSF explicitly lists — many FSF-acceptable-in-practice licenses (bzip2-1.0.6, libtiff, MIT-Open-Group) carry `False`; those need the curated `EXTRAS` route instead |
| Schema contract | keyed by SPDX id, not repo — exempt from the repo-keyed `repo_id` contract |
