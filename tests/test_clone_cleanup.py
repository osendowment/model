"""Tests for clone temp-dir auto-cleanup in src/git/disk.py."""

import os
import shutil
import time

from src.git.disk import CLONE_TMP_PREFIX, make_clone_tmpdir, sweep_stale_clone_dirs


def _aged_dir(parent, name, age_minutes):
    """Create `parent/name` with a leftover clone inside, mtime aged back."""
    p = parent / name
    p.mkdir()
    (p / "clone").mkdir()
    old = time.time() - age_minutes * 60
    os.utime(p, (old, old))
    return p


def test_sweep_removes_old_clone_dirs(tmp_path):
    stale = _aged_dir(tmp_path, f"{CLONE_TMP_PREFIX}scc-abc", age_minutes=180)
    removed = sweep_stale_clone_dirs(max_age_minutes=60, tmp_dir=str(tmp_path))
    assert removed == 1
    assert not stale.exists()


def test_sweep_keeps_recent_clone_dirs(tmp_path):
    # A recently-touched dir may be a live concurrent run — never delete it.
    fresh = _aged_dir(tmp_path, f"{CLONE_TMP_PREFIX}scc-xyz", age_minutes=5)
    removed = sweep_stale_clone_dirs(max_age_minutes=60, tmp_dir=str(tmp_path))
    assert removed == 0
    assert fresh.exists()


def test_sweep_ignores_non_matching_dirs(tmp_path):
    other = _aged_dir(tmp_path, "some-other-tmpdir", age_minutes=180)
    removed = sweep_stale_clone_dirs(max_age_minutes=60, tmp_dir=str(tmp_path))
    assert removed == 0
    assert other.exists()


def test_make_clone_tmpdir_uses_prefix():
    d = make_clone_tmpdir("scc")
    try:
        assert os.path.basename(d).startswith(f"{CLONE_TMP_PREFIX}scc-")
        assert os.path.isdir(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
