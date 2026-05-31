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
- `dependencies.csv` -- formula, dep_name, dep_type, fetched_at. Both `runtime` and `build` types are captured here, but the cpp pipeline filters to `runtime` only when building its dep tree (`src/cpp/process_data.py:277`).
- `downloads.csv` -- formula, year, downloads

## Scripts

| Script | Purpose |
|--------|---------|
| `src/homebrew/fetch_homebrew_data.py` | Fetch formulas + analytics |
| `src/homebrew/process_data.py` | Build outputs |

```bash
uv run src/homebrew/fetch_homebrew_data.py [--step formulas|analytics] [--years 2023 2024 2025]
uv run python -m src.homebrew.process_data [--include-all-langs]
```

## Outputs

In `data/sources/homebrew/`:
- `top-packages.csv`, `dependency-tree.csv`, `github-repos.csv`, `results.csv`
