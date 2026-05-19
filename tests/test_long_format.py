"""Tests for src/git/long_format.py."""

from src.git.long_format import read, upsert_rows, upsert_snapshot, write_all


def _row(repo, sha, metric, value, repo_id="1", checked_at="2026-01-01T00:00:00Z"):
    return {"repo": repo, "repo_id": repo_id, "commit_sha": sha,
            "metric": metric, "value": value, "checked_at": checked_at}


def test_upsert_replaces_by_key(tmp_path):
    p = tmp_path / "m.csv"
    write_all(p, [_row("a/b", "sha1", "loc", "10")])
    upsert_rows(p, [_row("a/b", "sha1", "loc", "99")])
    rows = read(p)
    assert rows[("a/b", "sha1", "loc")]["value"] == "99"


def test_upsert_skips_blank_value_and_sha(tmp_path):
    p = tmp_path / "m.csv"
    upsert_rows(p, [
        _row("a/b", "sha1", "loc", ""),     # blank value — skipped
        _row("a/b", "", "loc", "5"),         # blank sha — skipped
        _row("a/b", "sha1", "sloc", "5"),    # kept
    ])
    rows = read(p)
    assert set(rows) == {("a/b", "sha1", "sloc")}


def test_upsert_snapshot_formats_values(tmp_path):
    p = tmp_path / "m.csv"
    upsert_snapshot(p, repo="a/b", repo_id="1", commit_sha="sha1",
                    metrics={"avg": 4.5, "total": 42, "skip": None},
                    checked_at="2026-01-01T00:00:00Z")
    rows = read(p)
    assert rows[("a/b", "sha1", "total")]["value"] == "42"   # int, no .0
    assert rows[("a/b", "sha1", "avg")]["value"] == "4.5"
    assert ("a/b", "sha1", "skip") not in rows               # None dropped


def test_atomic_write_sweeps_orphan_temp_files(tmp_path):
    """A temp file left by a SIGKILLed write must not accumulate — the next
    write to the same path sweeps stale `<name>.*.tmp` siblings."""
    p = tmp_path / "m.csv"
    write_all(p, [_row("a/b", "sha1", "loc", "1")])
    orphan = tmp_path / "m.csv.deadbeef.tmp"
    orphan.write_text("partial write that was killed\n")
    assert orphan.exists()

    write_all(p, [_row("a/b", "sha1", "loc", "2")])  # next write sweeps it
    assert not orphan.exists()
    assert read(p)[("a/b", "sha1", "loc")]["value"] == "2"
