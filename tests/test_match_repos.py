"""Tests for src/sources/funding/match_repos.py — rename-robust host matching.

`classify()` attributes a foundation `host` to each github/repos.csv row by
matching the roster's declared `github_repo` slug against the repo. A repo
renamed on GitHub used to silently lose its institutional host because the raw
slug on one side (roster) never equalled the cached slug on the other
(repos.csv). The fix canonicalizes BOTH sides through
`src.common.repos.canonical_repo_map`, so a renamed repo still resolves.
"""

from src.sources.funding import match_repos as mr


def _write_repos(tmp_path, rows):
    """Write a minimal github/repos.csv (the columns classify + canonical_repo_map
    read) and return its path."""
    p = tmp_path / "repos.csv"
    lines = ["repo,full_name,repo_id,homepage"]
    lines += [",".join(r) for r in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_classify_matches_renamed_repo_when_roster_uses_current_name(tmp_path, monkeypatch):
    """Forward rename: repos.csv row is cached under the OLD slug
    (`microsoft/deepspeed`), but GitHub's `/repos` endpoint followed the rename so
    `full_name` is the current `deepspeedai/deepspeed`. The foundation roster lists
    the CURRENT name (`deepspeedai/deepspeed`).

    Before the fix, the raw-slug match compared the old repos.csv slug against the
    roster's new name, missed, and the repo lost its `host`. Canonicalizing both
    sides through repos.csv resolves it. This assertion FAILS on the unmodified
    source (empty host) and PASSES with the fix.
    """
    repos = _write_repos(tmp_path, [
        ("microsoft/deepspeed", "deepspeedai/deepspeed", "gh/1", ""),
    ])
    monkeypatch.setattr(mr, "REPOS_FILE", repos)

    rows = mr.classify(
        {"deepspeedai/deepspeed": "lf"},   # roster declares the NEW (current) name
        {},                                # org_idx
        {},                                # domain_idx
        {"lf": "2026-01-01"},              # fetched_at_by_host
    )
    row = next(r for r in rows if r["repo"] == "microsoft/deepspeed")
    assert row["host"] == "lf"                     # LF host recovered
    assert row["host_source"] == "project_list"


def test_classify_matches_renamed_repo_when_roster_uses_old_name(tmp_path, monkeypatch):
    """Reverse (vice-versa) rename: the foundation roster is STALE and still lists
    the OLD name (`microsoft/deepspeed`) while repos.csv carries the rename record
    (`repo` = the old cached slug, `full_name` = the current
    `deepspeedai/deepspeed`). Canonicalizing the roster key forward keeps the host
    attribution — the fix must never drop this match.

    NOTE ON THE FIXTURE: repos.csv must retain the old slug in its `repo` column
    for `canon` to bridge old→new. The link is information-theoretically
    unrecoverable if BOTH columns already hold the canonical name
    (`repo=full_name=deepspeedai/deepspeed`): nothing anywhere maps
    `microsoft/deepspeed` back to `deepspeedai/deepspeed`, so no implementation of
    this fix could resolve it. The realistic reverse record is therefore
    `repo`=old, `full_name`=new (what GitHub actually returns after a rename).
    """
    repos = _write_repos(tmp_path, [
        ("microsoft/deepspeed", "deepspeedai/deepspeed", "gh/1", ""),
    ])
    monkeypatch.setattr(mr, "REPOS_FILE", repos)

    rows = mr.classify(
        {"microsoft/deepspeed": "lf"},     # roster declares the OLD (stale) name
        {},                                # org_idx
        {},                                # domain_idx
        {"lf": "2026-01-01"},              # fetched_at_by_host
    )
    row = next(r for r in rows if r["repo"] == "microsoft/deepspeed")
    assert row["host"] == "lf"                     # stale-roster match preserved
    assert row["host_source"] == "project_list"
