"""One-shot: backfill contributor metrics for the 22 archived-but-eligible repos.

Background: load_ab_repos() filters out archived repos by default, but they
remain in the eligibility set, leaving holes in data/risk/concentration.csv. This
script calls batch_update directly with a hand-curated slug list, bypassing
the loader's archived filter.

Run from repo root:
    uv run python -m scripts.backfill_concentration_22
"""
import asyncio
import logging

from src.github.batch_runner import batch_update

REPOS = [
    "azure/msrest-for-python",
    "bincode-org/bincode",
    "bminor/glibc",
    "broofa/node-int64",
    "dominictarr/through",
    "dtolnay/paste",
    "eslint/doctrine",
    "facebook/prop-types",
    "facebook/regenerator",
    "fitzgen/glob-to-regexp",
    "floatdrop/require-from-string",
    "googleapis/google-auth-library-python",
    "googleapis/google-cloud-node-core",
    "googleapis/google-resumable-media-python",
    "googleapis/python-crc32c",
    "googleapis/python-storage",
    "jimmywarting/node-domexception",
    "lddubeau/xmlchars",
    "raynos/xtend",
    "sindresorhus/path-is-absolute",
    "sybrenstuvel/python-rsa",
    "webpack/loader-utils",
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await batch_update(
        REPOS,
        year_start=2021,
        year_end=2025,
        output="data/sources/github/contributors",
        force=True,  # bypass 90-day TTL gate (these have no row, but be safe)
    )


if __name__ == "__main__":
    asyncio.run(main())
