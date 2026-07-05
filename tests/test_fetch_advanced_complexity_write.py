"""Regression tests for fetch_advanced_complexity._write_results — no fake
zeros. Mirrors tests/test_fetch_cognitive_write.py: the cyclomatic twin was
missing the files==0 guard, so one empty/partial checkout persisted a real
cyclomatic_max=0 (half the complexity score) and the 365d TTL then shielded
the lie from re-analysis.
"""

from src.sources.git.long_format import read as read_long
from src.sources.github.fetch_advanced_complexity import (
    RepoComplexity,
    _write_results,
)


class TestWriteResults:
    def test_zero_files_result_not_persisted(self, tmp_path):
        path = str(tmp_path / "lizard.csv")
        results = [
            RepoComplexity(repo="big/repo", repo_id="1", analyzed_sha="a" * 40,
                           files=0),  # crashed / empty — must be skipped
            RepoComplexity(repo="good/repo", repo_id="2", analyzed_sha="b" * 40,
                           files=12, cyclomatic_total=340,
                           cyclomatic_avg=4.5, cyclomatic_max=29),
        ]
        _write_results(path, results)

        rows = read_long(path)
        repos = {repo for (repo, _sha, _metric) in rows}
        assert repos == {"good/repo"}
        assert rows[("good/repo", "b" * 40, "cyclomatic_max")]["value"] == "29"

    def test_error_and_missing_sha_still_skipped(self, tmp_path):
        path = str(tmp_path / "lizard.csv")
        _write_results(path, [
            RepoComplexity(repo="err/repo", repo_id="3", analyzed_sha="c" * 40,
                           files=5, error="boom"),
            RepoComplexity(repo="nosha/repo", repo_id="4", files=5),
        ])
        assert read_long(path) == {}
