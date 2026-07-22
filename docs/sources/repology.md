# Repology

Cross-distribution package name mapping. The C/C++ pipeline uses it to unify
Debian and Homebrew packages under one canonical project name.

## Data Source

**API**: `https://repology.org/api/v1/projects/{cursor}/?inrepo=<repo>` — a
cursor-paginated project list, 200 projects per page. No authentication.
The fetcher waits 1.2 s between requests to stay inside Repology's ~1 rps
fair-use limit, and retries 429/502/503/504 with a 5/15/30/60 s backoff.
A full crawl of both repos takes about 3 minutes.

## Why It's Needed

One upstream project carries a different name in each ecosystem:

| Ecosystem names | Repology project |
|---|---|
| boost1.74 / boost1.81 / boost1.83 (Debian) | `boost` |
| openssl@3 / openssl@3.0 / openssl@3.5 (Homebrew) | `openssl` |
| libpng1.6 (Debian) / libpng (Homebrew) | `libpng` |

The canonical name collapses this noise, so downloads and dependencies
aggregate correctly across ecosystems.

## Raw Data

`data/sources/repology/packages.csv` — one row per (project, repo, package):

| Column | Description |
|---|---|
| `project` | Repology canonical project name — the join key |
| `repo` | Source repo (`debian_13` \| `homebrew`) |
| `srcname` | Source package name in that repo |
| `binname` | Binary package name in that repo |
| `visiblename` | Name Repology displays |
| `version` | Packaged version |
| `status` | Repology version status (`newest`, `outdated`, …) |
| `categories` | Repo section/category list |
| `licenses` | Licenses the repo declares |
| `fetched_at` | UTC fetch timestamp |

`data/sources/repology/project-urls.csv` — git-URL candidates scraped from
Repology's per-project information pages (`project`, `candidate_url`,
`platform`, `status`, `fetched_at`). A different script writes it:
`src/sources/cpp/fetch_repology_urls.py`.

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/repology/fetch_repology_data.py` | Crawl the project list for the target repos → `packages.csv` |

```bash
uv run python -m src.sources.repology.fetch_repology_data                  # debian_13 + homebrew
uv run python -m src.sources.repology.fetch_repology_data --repo debian_13 # one repo
uv run python -m src.sources.repology.fetch_repology_data --refresh        # ignore the TTL
```

## Refresh

| Aspect | Behavior |
|---|---|
| TTL | 365 days (`fetch_ttl_days` in settings.json), gating the whole file — a warm re-run makes zero network calls |
| `--refresh` | Refetch past the TTL. The pipeline runner propagates it |
| Pipeline step | `repology` in `src.value.run_value_pipeline` (`net=True`) |

`src.sources.cpp.process_data` and `src.sources.cpp.check_eol` both read
`packages.csv`, so a stale file misjoins the C/C++ pipeline.
