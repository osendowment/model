"""Key-contributor lookup for data/preview/people.csv.

A thin, independently-named wrapper over `bf_contributors.load_bf_contributors`
— the underlying "top contributors covering a cumulative commit share" logic
is identical, but this module exists so `src.build_people` never references
`bf_contributors` by name. That module's threshold is deliberately pinned to
`concentration.bus_factor_threshold` and shared with two other consumers
(`fetch_maintainer_sponsors.py`, `build_funding.py`'s `bf_maintainer_fundable`
signal) — retuning it in place would change those two existing outputs. This
wrapper lets `data/preview/people.csv`'s "key contributor" role use its own
`key_contributors.cum_share` setting (see `src.common.params`) without
touching that shared definition.
"""
from __future__ import annotations

from src.sources.github import bf_contributors


def load_key_contributors(cum_share: float) -> dict[str, list[str]]:
    """Map ``repo_id`` -> key-contributor logins at the given cumulative
    commit-share threshold. See `bf_contributors.load_bf_contributors` for
    the membership rule itself."""
    return bf_contributors.load_bf_contributors(
        path=bf_contributors.CONTRIB_FILE, threshold=cum_share
    )
