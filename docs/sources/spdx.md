# SPDX License List (Linux Foundation)

The canonical machine-readable license registry, maintained by the
[SPDX project](https://spdx.dev/) under the Linux Foundation. The model stores
it in full — every SPDX id with both approval flags — as the single upstream
for its unified OSS-approved set ([osi.md](osi.md), built by
`src.sources.osi.fetch_licenses`).

## Data Source

**Source**: the [spdx/license-list-data](https://github.com/spdx/license-list-data)
JSON dump, fetched raw from
`raw.githubusercontent.com/spdx/license-list-data/main/json/licenses.json`.
No authentication — a single JSON download. SPDX ids match what
npm/PyPI/crates/Homebrew publishers actually declare.

Each license carries two independent review-body flags:

| Flag | Body | Meaning |
|------|------|---------|
| `isOsiApproved` | Open Source Initiative | Passed OSI's formal license review |
| `isFsfLibre` | Free Software Foundation | Listed as free on gnu.org/licenses/license-list (software AND content licenses) |

## Raw Data

`data/sources/spdx/licenses.csv` — one row per SPDX id. The fetcher filters
nothing; policy lives in the OSS-set builder.

| Column | Example | Description |
|--------|---------|-------------|
| `spdx_id` | `apache-2.0` | Lowercased SPDX id — the join key |
| `spdx_id_canonical` | `Apache-2.0` | Original SPDX casing |
| `name` | `Apache License 2.0` | Full human-readable name |
| `is_osi_approved` | `True` | SPDX `isOsiApproved` |
| `is_fsf_libre` | `True` | SPDX `isFsfLibre`; absent in the JSON → `False` |
| `is_deprecated` | `False` | SPDX deprecated the id |
| `reference` | `https://spdx.org/licenses/Apache-2.0.html` | SPDX page |
| `osi_url` | `https://opensource.org/license/apache-2.0` | First opensource.org link in `seeAlso` |
| `fetched_at` | `2026-07-06T15:00:00Z` | UTC timestamp, one per fetch run |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/spdx/fetch_licenses.py` | Fetch the list → `licenses.csv` |

```bash
uv run python -m src.sources.spdx.fetch_licenses
uv run python -m src.sources.spdx.fetch_licenses --force
```

## Refresh & Caveats

| Aspect | Behavior |
|--------|----------|
| TTL | 90 days. A re-run inside the window is a no-op unless you pass `--force` |
| Self-bootstrap | `ensure()` runs from the OSS-set builder and from the `spdx` step of `run_eligibility_pipeline` |
| `isFsfLibre` coverage | SPDX marks only the licenses the FSF explicitly lists. Licenses the FSF accepts in practice — bzip2-1.0.6, libtiff, MIT-Open-Group — carry `False`, so they need the curated `EXTRAS` route in [osi.md](osi.md) |
| Schema contract | Keyed by SPDX id, not by repo — exempt from the repo-keyed `repo_id` contract |
