# Eligibility Pipeline

## Data Sources

- `data/github/top-repos.csv` — repos to check for license eligibility

## Scripts

| Script | Purpose | Command |
|--------|---------|---------|
| `src/eligibility.py` | Classifies repos by OSS license eligibility (OSI-approved licenses) | `uv run python -m src.eligibility` |

## Outputs

### eligibility.csv

OSS license eligibility per repo.

| File | Description |
|------|-------------|
| `data/eligibility.csv` | OSS license eligibility per repo |
