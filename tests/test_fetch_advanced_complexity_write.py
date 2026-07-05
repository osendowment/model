"""Regression tests for fetch_sha_metrics._write_results — no fake zeros (the
cyclomatic view). The lizard writer must skip a `files == 0` result so one
empty/partial checkout can't persist a real cyclomatic_max=0 (half the
complexity score) that the 365d TTL then shields from re-analysis. The former
fetch_advanced_complexity writer merged into fetch_sha_metrics.
"""

from src.sources.git.long_format import read as read_long
from src.sources.git.fetch_sha_metrics import RepoResult, _write_results


class TestWriteResults:
    def test_zero_files_result_not_persisted(self, tmp_path):
        path = str(tmp_path / "lizard.csv")
        results = [
            RepoResult(repo="big/repo", repo_id="1", analyzed_sha="a" * 40,
                       files=0),  # crashed / empty — must be skipped
            RepoResult(repo="good/repo", repo_id="2", analyzed_sha="b" * 40,
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
            RepoResult(repo="err/repo", repo_id="3", analyzed_sha="c" * 40,
                       files=5, error="boom"),
            RepoResult(repo="nosha/repo", repo_id="4", files=5),
        ])
        assert read_long(path) == {}
