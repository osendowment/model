# C/C++ (unified Debian + Homebrew)

Unified pipeline for C/C++ libraries, joining Debian and Homebrew data at the
Repology project level. Not a data source itself -- combines data from
[Debian](debian.md), [Homebrew](homebrew.md), [Repology](repology.md), and
[OSS-Fuzz](ossfuzz.md).

## How It Works

Repology maps ecosystem-specific names to a canonical project name, then:

- **Downloads**: MAX within each ecosystem (avoid double-counting variants like boost1.74/1.81), SUM across ecosystems (Debian + Homebrew are disjoint user populations)
- **Dependencies**: union of **runtime-only** project-to-project edges from both ecosystems. Build-time and recommended deps are excluded — see below.
- **is_cpp**: True if any constituent binary/formula is flagged C/C++
- **Top selection**: within 95% cumulative download mass in **either** Debian or Homebrew

### Dependency types: runtime only

The cpp dep tree contains runtime project→project edges only. Build-time
tooling (cmake, pkgconf, autoconf, gettext, etc.) and Debian
`Recommends`/`Suggests` do **not** propagate PageRank. Two filters combine:

| Source | Collected at fetch | Filter applied by cpp |
|---|---|---|
| Debian (`fetch_debian_data.py`) | `Depends` + `Pre-Depends` only — already runtime-only. `Build-Depends`, `Recommends`, `Suggests` are not collected at all. | none (all of it is used) |
| Homebrew (`fetch_homebrew_data.py`) | both `runtime` and `build` types stored in raw deps | `cpp/process_data.py:277` — `if dep_type != "runtime": continue` |

Result: PageRank reflects who *runs* with whom, not who *builds* whom. The
`type` column in `data/cpp/dependency-tree.csv` is uniformly `"declared"` —
that label is the cpp pipeline's own term meaning "runtime dep declared by
either ecosystem", not a faithful preservation of the source-side type field.

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
