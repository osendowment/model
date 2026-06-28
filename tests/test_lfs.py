"""Tests for src.common.lfs — Git LFS pointer detection."""
from __future__ import annotations

from src.common.lfs import has_real_data, is_lfs_pointer

POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:948b34b08b3ef2b51a206540f30bba45010ab49b03bcd53fbe7f1b2ff5a41d89\n"
    "size 643974\n"
)


def test_pointer_file_detected(tmp_path):
    p = tmp_path / "crates.csv"
    p.write_text(POINTER)
    assert is_lfs_pointer(p) is True
    assert has_real_data(p) is False


def test_real_data_not_a_pointer(tmp_path):
    p = tmp_path / "crates.csv"
    p.write_text("id,name,repository\n1,rand,https://github.com/rust-random/rand\n")
    assert is_lfs_pointer(p) is False
    assert has_real_data(p) is True


def test_missing_file(tmp_path):
    p = tmp_path / "absent.csv"
    assert is_lfs_pointer(p) is False
    assert has_real_data(p) is False


def test_large_file_is_not_a_pointer(tmp_path):
    # A real file that merely happens to start with the magic text is still not
    # a pointer once it exceeds the tiny pointer-size bound.
    p = tmp_path / "big.csv"
    p.write_text(POINTER + "x" * 2048)
    assert is_lfs_pointer(p) is False
    assert has_real_data(p) is True


def test_directory_is_not_a_pointer(tmp_path):
    assert is_lfs_pointer(tmp_path) is False
    assert has_real_data(tmp_path) is False
