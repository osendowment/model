"""Regression tests for fetch_cognitive._write_results — no fake zeros."""

from src.git.long_format import read as read_long
from src.github.fetch_cognitive import RepoCognitive, _write_results


class TestWriteResults:
    def test_zero_files_result_not_persisted(self, tmp_path):
        """A `files == 0` result (empty/partial checkout, crashed worker)
        must NOT be written — RepoCognitive's zero defaults would land in
        lizard.csv as a real cognitive_total=0.
        """
        path = str(tmp_path / "lizard.csv")
        results = [
            RepoCognitive(repo="big/repo", repo_id="1", analyzed_sha="a" * 40,
                          files=0),  # crashed / empty — must be skipped
            RepoCognitive(repo="good/repo", repo_id="2", analyzed_sha="b" * 40,
                          files=12, cognitive_total=340,
                          cognitive_avg=4.5, cognitive_max=29),
        ]
        _write_results(path, results)

        rows = read_long(path)
        repos = {repo for (repo, _sha, _metric) in rows}
        assert repos == {"good/repo"}
        assert ("good/repo", "b" * 40, "cognitive_total") in rows
        assert rows[("good/repo", "b" * 40, "cognitive_total")]["value"] == "340"

    def test_error_and_missing_sha_still_skipped(self, tmp_path):
        path = str(tmp_path / "lizard.csv")
        results = [
            RepoCognitive(repo="err/repo", repo_id="1", analyzed_sha="c" * 40,
                          files=5, error="timeout"),
            RepoCognitive(repo="nosha/repo", repo_id="2", analyzed_sha="",
                          files=5, cognitive_total=10),
        ]
        _write_results(path, results)
        assert read_long(path) == {}

    def test_genuine_zero_complexity_is_persisted(self, tmp_path):
        """files > 0 with cognitive_total == 0 is a real measurement (a
        repo of branch-free code) — it must still be written.
        """
        path = str(tmp_path / "lizard.csv")
        results = [
            RepoCognitive(repo="trivial/repo", repo_id="1",
                          analyzed_sha="d" * 40, files=3,
                          cognitive_total=0, cognitive_avg=0.0,
                          cognitive_max=0),
        ]
        _write_results(path, results)
        rows = read_long(path)
        assert ("trivial/repo", "d" * 40, "files") in rows
        assert rows[("trivial/repo", "d" * 40, "files")]["value"] == "3"
