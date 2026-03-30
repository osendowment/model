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

- **Performance AND clarity** — scripts must be fast (async I/O, batching, concurrency) but also easy to read and audit. These are not in conflict: optimize with explicit, well-named code rather than clever tricks.
- A researcher unfamiliar with the codebase should be able to follow exactly what a script does and why — even if it uses async or batching.
- Data transformations must be traceable — it should always be clear where each output value came from.
- Prefer flat, explicit steps. Use abstractions only when they make the code *more* readable, not less.
- Name things clearly. A well-named function or variable is worth more than a comment.

## Stack

- Python with `uv` for package management (`uv run` to execute scripts)
- Async I/O with `aiohttp` for data fetching
- `rich` for terminal output (progress bars, tables)
