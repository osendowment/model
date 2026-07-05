"""Bus-factor contributor membership from the GitHub `/contributors` data.

A repo's **bus-factor set** is the fewest top contributors (by GitHub-method
lifetime contributions, bots excluded) whose commits cumulatively reach
`THRESHOLD` (50%) of the repo's total — exactly the population counted by
`build_concentration`'s `bf_commits_gh_alltime`. This module exposes *which*
logins are in that set, reusing the canonical `_compute_bus_factor` so the
membership can never drift from the count.

Two consumers share this one definition, so they agree on exactly which people
"carry" a repo:
  - `fetch_maintainer_sponsors` — which contributor logins to check for a
    personal GitHub Sponsors listing.
  - `build_funding` — which repos gain `bf_maintainer_fundable` (a maintainer
    who carries the repo is personally fundable → the repo has funding intent).

Restricting the signal to the bus-factor set (not *any* contributor) is what
keeps it honest: a drive-by contributor who merely happens to have Sponsors
cannot manufacture funding intent for a repo they did not write.

Reads: data/sources/github/contributor-commits.csv
    (repo, repo_id, login, contributions, account_type) — the GitHub
    `/contributors` method, one row per (repo, contributor).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from src.sources.github.fetch_contributors_metrics import _compute_bus_factor
from src.sources.github.models import THRESHOLD, Contributor

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
CONTRIB_FILE = DATA_DIR / "sources" / "github" / "contributor-commits.csv"


def _bf_member_logins(rows: list[dict], threshold: float = THRESHOLD) -> list[str]:
    """The bus-factor logins for one repo's `/contributors` rows.

    The population is exactly the one `build_concentration` counts as
    `bf_commits_gh_alltime`: `account_type != "Bot"` filtered here, then any
    `is_bot(login)` match dropped by `_compute_bus_factor` — so `bf` is the same
    integer. An anonymous contributor (empty login) is kept in that population
    (it can occupy a bus-factor slot, exactly as in the count) but dropped from
    the returned list, since a blank login can never match a fundable maintainer.
    Returns the top `bf` non-bot logins in contribution order (empty when the
    repo has no positive-commit human contributor).
    """
    contribs = [
        Contributor(login=(r.get("login") or "").strip().lower(),
                    lines_changed=0,
                    commits=int((r.get("contributions") or 0)))
        for r in rows
        if (r.get("account_type") or "").strip() != "Bot"
    ]
    if not contribs:
        return []
    bf, all_sorted, _hhi = _compute_bus_factor(
        contribs, threshold=threshold, base="commits", include_bots=False)
    # `all_sorted` still contains any is_bot(login) matches; re-drop them and
    # take the top `bf` — the same set the bus-factor count was computed over —
    # then drop blank (anonymous) logins, which are uncheckable.
    non_bot_sorted = [c for c in all_sorted if not c.is_bot]
    return [c.login for c in non_bot_sorted[:bf] if c.login]


def load_bf_contributors(path: Path = CONTRIB_FILE,
                         threshold: float = THRESHOLD) -> dict[str, list[str]]:
    """Map ``repo_id`` → its bus-factor contributor logins (lowercased).

    `repo_id` is the stable `gh/<n>` identity carried in contributor-commits.csv,
    so the membership survives a GitHub rename. Repos absent from the file (never
    fetched) are simply absent from the map.
    """
    by_repo: dict[str, list[dict]] = defaultdict(list)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            if rid:
                by_repo[rid].append(row)
    return {rid: members for rid, rows in by_repo.items()
            if (members := _bf_member_logins(rows, threshold))}
