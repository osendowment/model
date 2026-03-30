# Project Guidelines

## Code Organization

- All scripts must live in `src/` under the relevant ecosystem folder:
  - `src/npm/` — npm/JavaScript ecosystem scripts
  - `src/crates/` — crates.io/Rust ecosystem scripts
  - `src/pypi/` — PyPI/Python ecosystem scripts
  - `src/github/` — GitHub API scripts
- Only truly general-purpose scripts (not tied to any one ecosystem) go in a top-level `scripts/` folder
- Never leave scripts in a bare `scripts/` folder if they belong to a specific ecosystem

## Philosophy

- **Simplicity and transparency over performance** — scripts and data pipelines must be easy to read, understand, and audit. A researcher unfamiliar with the codebase should be able to follow exactly what a script does and why.
- Prefer flat, explicit steps over clever abstractions. If something can be done in a straightforward loop, don't wrap it in a framework.
- Data transformations must be traceable — it should always be clear where each output value came from.
- Avoid premature optimization. Correctness and clarity come first.

## Stack

- Python with `uv` for package management (`uv run` to execute scripts)
- Async I/O with `aiohttp` for data fetching
- `rich` for terminal output (progress bars, tables)
