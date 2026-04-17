# Repology

Cross-distribution package name mapping. Used by the C/C++ pipeline to unify
Debian and Homebrew packages under a single canonical project name.

## Data Source

**API**: [repology.org/api/v1/projects](https://repology.org/api/v1/projects) -- paginated project list. Fair-use rate limit (~1 req/s, enforced at 1.2s delay). Full crawl takes ~3 minutes.

No authentication required.

## Why It's Needed

The same upstream project appears under different names in different ecosystems:
- boost1.74 / boost1.81 / boost1.83 (Debian) -> **boost** (Repology)
- openssl@3 / openssl@3.0 / openssl@3.5 (Homebrew) -> **openssl** (Repology)
- libpng1.6 (Debian) / libpng (Homebrew) -> **libpng** (Repology)

Repology's canonical project name collapses this noise so downloads and deps
can be aggregated correctly across ecosystems.

## Raw Data

- `data/repology/packages.csv` -- project, repo, srcname, binname, visiblename, version, status, categories, licenses

## Scripts

| Script | Purpose |
|--------|---------|
| `src/repology/fetch_repology_data.py` | Crawl project list for target repos |

```bash
uv run src/repology/fetch_repology_data.py [--repo debian_13|homebrew]
```
