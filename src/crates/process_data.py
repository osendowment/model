"""
Process the crates.io DB dump into top-package-downloads.csv.

Reads from the most recent data/crates/dump-YYYYMMDD/ folder and produces
data/crates/top-package-downloads.csv with per-year download counts.

Note: version_downloads.csv in the dump only covers ~90 days of rolling history.
      For full yearly data (2021-2025) this script also reads pre-aggregated
      per-year files from .cache/crates/downloads_{year}.csv when available.

Run:
    uv run src/crates/process_data.py
    uv run src/crates/process_data.py --dump-dir data/crates/dump-20260329
"""

# TODO: implement
raise NotImplementedError("process_data.py not yet implemented")
