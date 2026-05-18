"""Tests for src/github/fetch_advanced_complexity.py."""

from src.github.fetch_advanced_complexity import (
    LIZARD_SKIP_SUFFIXES,
    MAX_FILE_BYTES,
    _list_source_files,
    _run_lizard,
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


def test_run_lizard_excludes_fortran(tmp_path):
    """lizard's Fortran reader OOM-kills on large fixed-form sources — scipy's
    359 KB ODRPACK `d_odr.f` was the lone repo we could never analyze. Fortran
    files must be dropped before they ever reach `analyze_files`.

    lizard *does* count Fortran subroutines as functions (verified), so a `.f`
    file alone would yield a function if it weren't excluded. The fix means it
    contributes nothing — adding the Fortran file leaves the result unchanged.
    """
    py = tmp_path / "m.py"
    py.write_text("def a(x):\n    if x:\n        return 1\n    return 0\n")
    fortran = tmp_path / "legacy.f"
    fortran.write_text(
        "      SUBROUTINE FOO(X)\n"
        "      INTEGER X\n"
        "      IF (X.GT.0) X = 1\n"
        "      RETURN\n"
        "      END\n"
    )

    # A Fortran-only input yields nothing — the file never reaches lizard.
    assert _run_lizard([str(fortran)]) == (0, 0, 0.0, 0)

    # Adding the Fortran file to a real input changes nothing.
    only_py = _run_lizard([str(py)])
    with_fortran = _run_lizard([str(py), str(fortran)])
    assert only_py == with_fortran
    assert only_py[0] == 1  # just a()


def test_fortran_skip_set_covers_source_exts():
    """Every Fortran extension in the sparse-checkout set is on the skip list —
    otherwise a Fortran file could slip through to lizard and OOM the run."""
    from src.git.clone import SOURCE_EXTS

    fortran_source_exts = {
        ext.lstrip("*").lower()
        for ext in SOURCE_EXTS
        if ext.lstrip("*").lower().startswith(".f")
    }
    assert fortran_source_exts
    assert fortran_source_exts <= LIZARD_SKIP_SUFFIXES
