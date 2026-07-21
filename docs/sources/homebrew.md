# Homebrew

Formula install counts, dependencies, and metadata from the macOS package
manager. One of the two inputs to the [C/C++ pipeline](../components/cpp.md),
alongside [Debian](debian.md).

## Data Sources

| Signal | Endpoint | Notes |
|---|---|---|
| Formula metadata | [formulae.brew.sh/api/formula.json](https://formulae.brew.sh/api/formula.json) | Every formula: name, description, homepage, source URL, license, language, dependencies |
| Install analytics | [formulae.brew.sh/api/analytics/install/365d.json](https://formulae.brew.sh/api/analytics/install/365d.json) | 365-day install counts. Historical years come from Wayback captures, which are often truncated at 1 MB |

`install-on-request/365d.json` serves as the analytics fallback. No
authentication required.

## Raw Data

In `data/sources/homebrew/raw/`:

| File | Schema | Notes |
|---|---|---|
| `formulas.csv` | `name, tap, desc, license, homepage, source_url, language` | |
| `dependencies.csv` | `formula, dep_name, dep_type, fetched_at` | Both `runtime` and `build` types are stored. The cpp pipeline keeps `runtime` only (`build_homebrew_edges()` in `src/sources/cpp/process_data.py`) |
| `downloads.csv` | `formula, year, downloads` | |
| `formula-api.json` | raw `formula.json` payload | Written and read by `src/sources/cpp/check_eol.py`, not by `fetch_homebrew_data.py`. It supplies each formula's `disabled` / `deprecated` flag. `python -m src.sources.cpp.check_eol --refresh` refetches it |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/homebrew/fetch_homebrew_data.py` | Two steps: `formulas` (formula.json) and `analytics` (Wayback install counts) |
| `src/sources/homebrew/process_data.py` | Build the outputs |
| `src/sources/homebrew/fetch_licenses.py` | Join the `license` column of `raw/formulas.csv` into `results.csv`, lower-cased |

```bash
uv run python -m src.sources.homebrew.fetch_homebrew_data [--step formulas|analytics|all] [--years 2023 2024 2025] [--limit N] [--refresh] [--offline]
uv run python -m src.sources.homebrew.process_data
uv run python -m src.sources.homebrew.fetch_licenses
```

Each fetch step carries a 7-day output TTL. A warm run skips it; `--refresh`
forces the refetch and `--offline` blocks the network entirely.
`fetch_licenses` runs as the `homebrew-lic` step of
`src.eligibility.run_eligibility_pipeline`, before `cpp-lic` — the cpp license
join reads Homebrew's result.

## Outputs

In `data/sources/homebrew/`: `top-packages.csv`, `dependency-tree.csv`,
`github-repos.csv`, `git.csv`, `results.csv`.

`results.csv` columns: `package`, `github_repo`, `git`, `eco_guess`,
`language`, `avg_downloads`, `2021`–`2025`, `top`, `is_cpp`, `is_oss_fuzz`,
`pagerank`, `value_class`, `license`.

## Year snapshots

A snapshot taken on date D reports installs for the 365 days *ending* at D, so
each target year uses a capture from the following year.
`YEAR_SNAPSHOT_OVERRIDES` in `fetch_homebrew_data.py` pins them:

| Year | Wayback timestamp | Capture |
|---|---|---|
| 2022 | `20230509130705` | 365d ending May 2023 |
| 2023 | `20230930071333` | 365d ending Sep 2023, 1 MB-truncated |
| 2024 | `20250121094300` | 365d ending Jan 2025, 1 MB-truncated |
| 2025 | `20251205062935` | 365d ending Dec 2025, full |

No usable 2021 snapshot exists, so `downloads.csv` starts at 2022. Years
without a pin fall back to the closest capture between Jun 1 of that year and
Jun 30 of the next.

## Limitations

- **Analytics are opt-in.** Users disable them with `brew analytics off`, so
  the counts are a fraction of real installs.
- **Rolling 365-day windows.** A year label names a window, not a calendar
  year — see the table above.
- **Truncated snapshots.** Captures cut at exactly 1 MB yield only the
  high-install head, recovered by regex parsing.
- **Not comparable to npm/PyPI/crates.** These are macOS install events, not
  cross-platform package downloads.
