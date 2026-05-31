# Debian

Package install counts, dependencies, and metadata from the Debian Linux distribution.
Used as one of two inputs (alongside Homebrew) for the C/C++ ecosystem pipeline.

## Data Sources

**Package popularity (popcon)**: Historical install statistics from [popcon.debian.org](https://popcon.debian.org), fetched via Wayback Machine snapshots. One snapshot per year.

**Package index**: [deb.debian.org](https://deb.debian.org/debian/dists/stable/main/binary-amd64/Packages.xz) -- latest dependency edges, homepage, and VCS metadata.

**C/C++ classification**: [UDD (Debian Package Database)](https://udd-mirror.debian.net) -- PostgreSQL mirror queried for debtags (`implemented-in::c`, `implemented-in::c++`). Public access: `host=udd-mirror.debian.net user=udd-mirror password=udd-mirror`.

No authentication required. Wayback snapshots may be sparse for some years.

## Raw Data

In `data/sources/debian/raw/`:
- `downloads.csv` -- binary, year, downloads (from popcon)
- `dependencies.csv` -- binary, dep_name. **Runtime only**: combines `Depends` + `Pre-Depends` from each binary's stanza. `Build-Depends`, `Recommends`, `Suggests` are intentionally not collected.
- `package-metadata.csv` -- binary, source, homepage, vcs_browser, section
- `cpp-packages.csv` -- debtags-identified C/C++ binaries
- `aliases.csv` -- t64 version renames (current <-> old)

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/debian/fetch_debian_data.py` | Multi-step: packages, popcon, index |
| `src/sources/debian/process_data.py` | Build outputs (source-level aggregation) |

```bash
uv run src/sources/debian/fetch_debian_data.py [--step packages|popcon|index] [--years 2023 2024 2025]
uv run python -m src.sources.debian.process_data
```

## Key Design

Aggregation is at **source package** level (not binary):
- Multiple binaries per source (e.g. ffmpeg -> libavcodec61, libavformat61)
- Downloads: MAX across binaries (avoid double-counting co-installed packages)
- Dependencies: binary-to-binary edges translated to source-to-source, deduplicated
- is_cpp: any binary flagged C/C++ -> entire source flagged

## Outputs

In `data/sources/debian/`:
- `top-packages.csv`, `dependency-tree.csv`, `github-repos.csv`, `results.csv`
