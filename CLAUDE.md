# Project Guidelines

## Code Organization

- All scripts must live in `src/` under the relevant ecosystem folder:
  - `src/npm/` — npm/JavaScript ecosystem scripts
  - `src/crates/` — crates.io/Rust ecosystem scripts
  - `src/pypi/` — PyPI/Python ecosystem scripts
  - `src/github/` — GitHub API scripts
- Only truly general-purpose scripts (not tied to any one ecosystem) go in a top-level `scripts/` folder
- Never leave scripts in a bare `scripts/` folder if they belong to a specific ecosystem

## Stack

- Python with `uv` for package management (`uv run` to execute scripts)
- Async I/O with `aiohttp` for data fetching
- `rich` for terminal output (progress bars, tables)
