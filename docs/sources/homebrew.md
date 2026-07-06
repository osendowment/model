# Homebrew

Formula install counts, dependencies, and metadata from the macOS package manager.
Used as one of two inputs (alongside Debian) for the C/C++ ecosystem pipeline.

## Data Sources

**Formula metadata**: [formulae.brew.sh/api/formula.json](https://formulae.brew.sh/api/formula.json) -- all formulas with name, description, homepage, source URL, license, language, and dependencies.

**Install analytics**: [formulae.brew.sh/api/analytics/install/365d.json](https://formulae.brew.sh/api/analytics/install/365d.json) -- 365-day install counts. Historical snapshots from Wayback Machine. Snapshots may be truncated (1 MB limit).

No authentication required.

## Raw Data

In `data/sources/homebrew/raw/`:
- `formulas.csv` -- name, tap, desc, license, homepage, source_url, language
- `dependencies.csv` -- formula, dep_name, dep_type, fetched_at. Both `runtime` and `build` types are captured here, but the cpp pipeline filters to `runtime` only when building its dep tree (`build_homebrew_edges()` in `src/sources/cpp/process_data.py`).
- `downloads.csv` -- formula, year, downloads

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/homebrew/fetch_homebrew_data.py` | Fetch formulas + analytics |
| `src/sources/homebrew/process_data.py` | Build outputs |

```bash
uv run src/sources/homebrew/fetch_homebrew_data.py [--step formulas|analytics|all] [--years 2023 2024 2025] [--limit N]
uv run python -m src.sources.homebrew.process_data
```

## Outputs

In `data/sources/homebrew/`:
- `top-packages.csv`, `dependency-tree.csv`, `github-repos.csv`, `results.csv`

## Limitations

- **Opt-in analytics** -- users can disable with `brew analytics off`; numbers are a
  fraction of actual installs.
- **Rolling 365-day windows** -- each snapshot represents "installs in the 365 days
  ending at snapshot date", not a calendar year. A May 2023 snapshot is used as proxy
  for "2022" but actually covers Jun 2022 -- May 2023.
- **Sparse + truncated snapshots** -- Wayback coverage is thin; some captures are
  truncated at exactly 1 MB (only the high-install head is recoverable via regex
  parsing). No usable 2021 snapshot exists.
- **Not comparable to npm/PyPI/crates** -- represents macOS install events, not
  cross-platform package downloads.

Available Wayback snapshots for `analytics/install/365d.json` (2023--2026):

```
2023: May 09, May 31, Sep 30
2024: May 22, Oct 07
2025: Jan 21, Apr 27, Sep 11, Dec 05
2026: Mar 06
```
No snapshots before 2023. No `install-on-request` snapshots before Sep 2022.
