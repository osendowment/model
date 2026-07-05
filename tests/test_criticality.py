"""Tests for src/sources/openssf/criticality.py."""


def test_heal_rows_backfills_repo_id_and_drops_renamed_duplicate():
    """A rename leaves two rows for one repo_id: the old slug's stale error
    row and the new slug's fresh ok row (whose repo_id was unresolvable at
    fetch time). Healing must backfill the id and keep only the scope row.
    Regression: facebook/react (error) vs react/react (ok, empty repo_id).
    """
    from src.sources.openssf.criticality import _heal_rows
    rows = {
        "facebook/react": {"repo": "facebook/react", "repo_id": "10270250",
                           "status": "error", "checked_at": "2026-06-29T17:46:22+00:00"},
        "react/react": {"repo": "react/react", "repo_id": "",
                        "status": "ok", "checked_at": "2026-07-05T15:40:00+00:00"},
    }
    healed = _heal_rows(rows, {"react/react": "10270250"}, scope={"react/react"})
    assert healed == 2                       # 1 backfill + 1 drop
    assert list(rows) == ["react/react"]
    assert rows["react/react"]["repo_id"] == "10270250"


def test_heal_rows_is_idempotent_and_keeps_distinct_repos():
    """A second healing pass finds nothing; rows with distinct ids or
    unresolvable slugs are left alone (no invented ids, no drops)."""
    from src.sources.openssf.criticality import _heal_rows
    rows = {
        "a/a": {"repo": "a/a", "repo_id": "1", "status": "ok", "checked_at": "2026-01-01"},
        "b/b": {"repo": "b/b", "repo_id": "2", "status": "ok", "checked_at": "2026-01-01"},
        "c/c": {"repo": "c/c", "repo_id": "", "status": "error", "checked_at": "2026-01-01"},
    }
    assert _heal_rows(rows, {}, scope={"a/a", "b/b"}) == 0
    assert len(rows) == 3
    assert rows["c/c"]["repo_id"] == ""      # unresolvable stays blank, not invented


def test_value_repo_ids_canonical_gh_form(tmp_path):
    """value.csv carries platform-prefixed ids (gh/123); the fallback map
    must expose canonical gh/<id> ids keyed by the canonical slug."""
    from src.sources.openssf.criticality import _value_repo_ids
    f = tmp_path / "value.csv"
    f.write_text(
        'repo,platform,repo_id\n'
        'react/react,github,gh/10270250\n'
        'gitlab/thing,gitlab,gl/999\n'
        'no-id/repo,github,\n'
    )
    ids = _value_repo_ids(str(f))
    assert ids == {"react/react": "gh/10270250"}
