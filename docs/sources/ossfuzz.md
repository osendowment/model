# OSS-Fuzz

Google's continuous fuzzing service for open-source projects. Used as a signal
in the C/C++ pipeline to identify projects with active fuzz testing.

## Data Source

**Source**: [github.com/google/oss-fuzz](https://github.com/google/oss-fuzz) --
downloaded as a tarball. Each project carries a `project.yaml` with language,
repo URL, and homepage. No authentication, one request per run.

**Freshness**: the whole index arrives in one download, so the TTL is
whole-file — `TTL_DAYS = 30` on `projects.csv` (`file_is_fresh`). A re-run
inside the window downloads nothing. `--refresh` ignores the TTL; `--offline`
never touches the network.

## Raw Data

- `data/sources/ossfuzz/projects.csv` -- project, language, github_repo, repo_id, main_repo, homepage, fetched_at

`repo_id` is the stable GitHub numeric id resolved from the `github_repo` slug
(`github/repos.csv` first, `value.csv` fallback); blank when the slug is out of
model scope — an id is never invented. It makes the downstream enrollment join
in `build_security` rename-proof; blank-id rows fall back to canonical-slug
matching. `fetched_at` is one UTC timestamp per fetch run (backfilled rows
carry the file's last data-commit date).

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/ossfuzz/fetch_ossfuzz_data.py` | Extract project metadata from oss-fuzz repo |

```bash
uv run python -m src.sources.ossfuzz.fetch_ossfuzz_data
uv run python -m src.sources.ossfuzz.fetch_ossfuzz_data --refresh   # ignore the 30-day TTL
uv run python -m src.sources.ossfuzz.fetch_ossfuzz_data --offline   # cache only, no network
```
