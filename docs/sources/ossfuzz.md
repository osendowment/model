# OSS-Fuzz

Google's continuous fuzzing service for open-source projects. Used as a signal
in the C/C++ pipeline to identify projects with active fuzz testing.

## Data Source

**Source**: [github.com/google/oss-fuzz](https://github.com/google/oss-fuzz) -- downloaded as a tarball. Each project has a `project.yaml` with language, repo URL, and homepage.

No authentication required (single tarball download).

## Raw Data

- `data/sources/ossfuzz/projects.csv` -- project, language, github_repo, main_repo, homepage

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/ossfuzz/fetch_ossfuzz_data.py` | Extract project metadata from oss-fuzz repo |

```bash
uv run src/sources/ossfuzz/fetch_ossfuzz_data.py
```
