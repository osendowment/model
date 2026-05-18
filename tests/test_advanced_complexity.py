"""Tests for src/github/fetch_advanced_complexity.py."""

from src.github.fetch_advanced_complexity import (
    MAX_FILE_BYTES,
    _list_source_files,
    analyze_directory,
)


def test_list_source_files_skips_oversized(tmp_path):
    """A source file larger than MAX_FILE_BYTES (minified/generated) is skipped
    — that's the OOM trigger that crashed lizard on mega-repos."""
    small = tmp_path / "small.py"
    small.write_text("def f():\n    return 1\n")
    big = tmp_path / "big.py"
    big.write_bytes(b"x = 1\n" * (MAX_FILE_BYTES // 6 + 100))
    assert big.stat().st_size > MAX_FILE_BYTES

    names = {p.rsplit("/", 1)[-1] for p in _list_source_files(str(tmp_path))}
    assert "small.py" in names
    assert "big.py" not in names


def test_list_source_files_only_source_extensions(tmp_path):
    (tmp_path / "code.py").write_text("x = 1\n")
    (tmp_path / "data.json").write_text("{}\n")
    (tmp_path / "readme.md").write_text("# hi\n")
    names = {p.rsplit("/", 1)[-1] for p in _list_source_files(str(tmp_path))}
    assert names == {"code.py"}


def test_analyze_directory_returns_cc_metrics(tmp_path):
    (tmp_path / "m.py").write_text(
        "def branchy(x):\n"
        "    if x > 0:\n"
        "        return 1\n"
        "    elif x < 0:\n"
        "        return -1\n"
        "    return 0\n"
    )
    out = analyze_directory(str(tmp_path))
    assert out["files"] == 1
    assert out["cyclomatic_total"] >= 3   # if / elif → CC ≥ 3
    assert out["cyclomatic_max"] >= 3
    assert out["cyclomatic_avg"] > 0


def test_analyze_directory_empty(tmp_path):
    assert analyze_directory(str(tmp_path)) == {"files": 0}
