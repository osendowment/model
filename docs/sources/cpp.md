# C/C++ (unified Debian + Homebrew)

Unified pipeline for C/C++ libraries, joining Debian and Homebrew data at the
Repology project level. Not a data source itself -- combines data from
[Debian](debian.md), [Homebrew](homebrew.md), [Repology](repology.md), and
[OSS-Fuzz](ossfuzz.md).

## How It Works

Repology maps ecosystem-specific names to a canonical project name, then:

- **Downloads**: MAX within each ecosystem (avoid double-counting variants like boost1.74/1.81), SUM across ecosystems (Debian + Homebrew are disjoint user populations)
- **Dependencies**: union of project-to-project edges from both ecosystems
- **is_cpp**: True if any constituent binary/formula is flagged C/C++
- **Top selection**: within 95% cumulative download mass in **either** Debian or Homebrew

## Scripts

| Script | Purpose |
|--------|---------|
| `src/cpp/process_data.py` | Unified C/C++ pipeline |

```bash
uv run python -m src.cpp.process_data [--top-share F] [--include-non-cpp]
```

## Outputs

In `data/cpp/`:
- `raw/packages.csv` -- per-project join with aggregated signals
- `top-packages.csv` -- top C/C++ projects by download mass
- `dependency-tree.csv` -- project-to-project dependency edges
- `github-repos.csv` -- project-to-GitHub-repo mappings
- `results.csv` -- all dep-tree projects with pagerank + value_class
