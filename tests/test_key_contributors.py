"""Tests for src/sources/github/key_contributors.py."""
from src.sources.github import key_contributors as kc


def test_load_key_contributors_delegates_to_bf_contributors(tmp_path, monkeypatch):
    """A thin, independently-named wrapper — same underlying membership
    logic as bf_contributors, called with an independently-tunable share."""
    contrib_file = tmp_path / "contributor-commits.csv"
    contrib_file.write_text(
        "repo_id,login,contributions,account_type\n"
        "gh/1,alice,90,User\n"
        "gh/1,bob,10,User\n",
        encoding="utf-8",
    )
    from src.sources.github import bf_contributors
    monkeypatch.setattr(bf_contributors, "CONTRIB_FILE", contrib_file)

    result = kc.load_key_contributors(cum_share=0.5)
    assert result == {"gh/1": ["alice"]}
