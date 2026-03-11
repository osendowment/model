## Stack

- **Python 3.13** with **uv** for package management — always use `uv run` to execute scripts (never bare `python`)
- **Pydantic** for data models and validation
- **Ruff** for linting and formatting
- **pytest** for testing

## Project Structure

- `src/github/` — GitHub API modules (search, contributor metrics)
- `src/risk.py` — Risk classification aggregation
- `data/github/` — GitHub datasets (repos, metrics, search counts)
- `data/pypi/` — PyPI datasets
- `tests/` — Test suite
