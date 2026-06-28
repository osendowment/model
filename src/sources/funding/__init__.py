"""FOSS foundation project lists.

Each module fetches the project roster of one major FOSS foundation
(Apache, CNCF, Eclipse, Linux Foundation, NumFOCUS, SFC) and writes it to
`data/sources/funding/{slug}/projects.csv`. `match_repos.py` then joins these
against `data/sources/github/search/top-repos.csv` to determine each repo's `host`
(slug of the foundation that hosts it, or empty).
"""
