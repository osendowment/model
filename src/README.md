# contrib-metrics

`contrib_metrics.py` computes contributor concentration metrics for any GitHub repository — primarily **bus factor** and **HHI (Herfindahl-Hirschman Index)**.

## How it works

The script fetches contributor stats from the GitHub API (or via `git clone` as fallback), then calculates two key metrics:

### Bus Factor

Bus factor answers: **how many top contributors account for 50% of the work?**

**Calculation (default: commit-based):**

1. Exclude bots from the contributor list
2. Compute each contributor's share: `pct = contributor_commits / total_commits`
3. Sort contributors by commits descending
4. Walk down the sorted list, accumulating percentages
5. Count how many contributors it takes to reach the **50% threshold**

That count is the bus factor.

```
Example: 3 contributors with 60%, 25%, 15% of commits
  - Contributor 1: cumulative = 60% → >= 50%, stop
  - Bus factor = 1
```

The `--base locs` flag switches from commits to lines changed (additions + deletions).

### HHI (Herfindahl-Hirschman Index)

HHI measures **contributor concentration** — how evenly work is distributed.

**Calculation:**

```
HHI = sum(pct_i^2) for each contributor i
```

Where `pct_i` is each contributor's share of total commits (or lines changed with `--base locs`).

| HHI value | Interpretation |
|-----------|---------------|
| `1/N` (e.g. 0.10 for 10 contributors) | Perfectly equal distribution |
| `< 0.15` | Low concentration (healthy) |
| `0.15 – 0.25` | Moderate concentration |
| `> 0.25` | High concentration |
| `1.0` | Single contributor (monopoly) |

### Configurable parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--threshold` | `0.5` | Cumulative share cutoff for bus factor |
| `--base` | `commits` | Metric basis: `commits` or `locs` |
| `--include-bots` | `false` | Include bot accounts in calculations |
| `--year` / `--year-end` | all time | Filter to a specific year range |

## Usage

```bash
# Single repo
uv run python -m src.contrib_metrics facebook/react

# Multiple repos from CSV
uv run python -m src.contrib_metrics --csv data/repos.csv

# Year breakdown
uv run python -m src.contrib_metrics facebook/react --yearly

# Lines-of-code basis
uv run python -m src.contrib_metrics facebook/react --base locs
```
