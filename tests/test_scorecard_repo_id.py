"""Regression test for scorecard repo_id stamping across renames.

``src/sources/openssf/scorecard`` stamps each fetched row's ``repo_id`` from a
slug -> id map. github/repos.csv alone is insufficient: a class-A repo already
in value.csv under its new canonical slug — but not yet re-fetched into
repos.csv — resolves to a blank id and is dropped by the id-keyed long-format
join in build_security. ``_repo_id_map`` overlays value.csv ids under the
repos.csv map (repos.csv wins on conflict) so a repo renamed since either file
was written still resolves.
"""
from __future__ import annotations

import src.sources.openssf.scorecard as sc


def test_repo_id_map_merges_value_overlay_repos_wins(monkeypatch):
    # repos.csv (github/repos.csv) map: a known slug + a conflicting slug.
    monkeypatch.setattr(
        sc, "load_repo_id_map",
        lambda *a, **k: {"repos-known/x": "gh/1", "conflict/x": "gh/REPOS"})
    # value.csv overlay: a value-only slug (the rename case) + the same conflict.
    monkeypatch.setattr(
        sc, "load_value_repo_ids",
        lambda *a, **k: {"value-only/y": "gh/2", "conflict/x": "gh/VALUE"})

    assert sc._repo_id_map() == {
        "repos-known/x": "gh/1",   # repos.csv slug resolves
        "value-only/y": "gh/2",    # value-only (renamed) slug resolves — the fix
        "conflict/x": "gh/REPOS",  # repos.csv wins the conflict
    }
