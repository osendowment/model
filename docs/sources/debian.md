# Debian

Package install counts, dependencies, and metadata from the Debian Linux
distribution. One of the two inputs to the [C/C++ pipeline](cpp.md),
alongside [Homebrew](homebrew.md).

## Data Sources

| Signal | Source | Notes |
|---|---|---|
| Package popularity | [popcon.debian.org](https://popcon.debian.org) `by_inst.gz`, via the Wayback CDX API | One snapshot per year — the capture closest to Dec 31 inside that calendar year |
| Package index | [deb.debian.org](https://deb.debian.org/debian/dists/stable/main/binary-amd64/Packages.xz) | Latest dependency edges, `Homepage`, `Vcs-Browser`, `Section` |
| C/C++ classification | [UDD](https://udd-mirror.debian.net) — the Debian Package Database Postgres mirror | Public read access: `host=udd-mirror.debian.net user=udd-mirror password=udd-mirror dbname=udd` |

The C/C++ set is the union of three UDD signals, and the `via` column records
which one matched:

1. Binaries debtagged `implemented-in::c` or `implemented-in::c++`.
2. Binaries whose *source* package carries that tag.
3. Binaries in the `libs`, `libdevel`, or `oldlibs` sections of current stable.

No authentication required.

## Raw Data

In `data/sources/debian/raw/`. `package` means the binary package name
throughout.

| File | Schema | Notes |
|---|---|---|
| `downloads.csv` | `package, year, downloads` | From popcon |
| `dependencies.csv` | `package, dep_name, dep_version, fetched_at` | **Runtime only** — `Depends` + `Pre-Depends`. `Build-Depends`, `Recommends`, and `Suggests` are never collected |
| `package-metadata.csv` | `package, source, homepage, vcs_browser, section` | |
| `cpp-packages.csv` | `package, tag, via` | C/C++ binaries from the UDD signal union |
| `aliases.csv` | `current, old` | t64 version renames |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/debian/fetch_debian_data.py` | Three steps: `packages` (UDD), `popcon` (Wayback), `index` (`Packages.xz`) |
| `src/sources/debian/process_data.py` | Build the outputs, aggregated at source-package level |

```bash
uv run python -m src.sources.debian.fetch_debian_data [--step packages|popcon|index|all] [--years 2023 2024 2025] [--limit N] [--refresh] [--offline]
uv run python -m src.sources.debian.process_data
```

Each fetch step carries a 7-day output TTL. A warm run skips it; `--refresh`
forces the refetch and `--offline` blocks the network entirely.

## Key Design

Aggregation happens at **source package** level, not binary level. One source
emits many binaries (ffmpeg → libavcodec61, libavformat61), and binaries get
renamed at every SONAME bump, so the source is the stable project unit.

| Field | Rule |
|---|---|
| Downloads | MAX across the source's binaries. SUM would double-count a machine holding `libfoo.so.5` and `libfoo.so.6` side by side |
| Dependencies | Binary→binary edges translated to source→source, self-loops dropped, then deduplicated |
| `is_cpp` | Any binary flagged C/C++ flags the whole source |
| `github` | First non-empty `github.com` URL across the binaries (`Homepage` or `Vcs-Browser`) |

## Outputs

In `data/sources/debian/`: `top-packages.csv`, `dependency-tree.csv`,
`github-repos.csv`, `results.csv` — all keyed by source package, all written
by `process_data.py`.

`data/sources/debian/git.csv` sits alongside them but the **value stage**
writes it (`src.value.build_git_urls`), not `process_data.py`. It re-reads
`raw/package-metadata.csv` and classifies every `Vcs-Browser` / `Homepage` URL
by host — `github`, `gitlab`, `bitbucket`, `sourcehut`, `codeberg`, `custom` —
so a Debian source whose upstream lives on salsa.debian.org or another GitLab
instance still reaches Risk and Eligibility. The `github` field in the Key
Design table above records only the GitHub subset.

## Limitations

- **Popcon is opt-in.** Only participating machines report, so the numbers
  sample the installed base rather than count it.
- **Wayback coverage is uneven.** Some years offer several usable captures of
  `by_inst.gz` and some offer one. Each year takes the capture closest to
  Dec 31, which makes the year label a rough proxy.
- **Not comparable to package-manager downloads.** Popcon measures "machines
  with the package installed", not download events.
