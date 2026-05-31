"""Tests for src/value/build_stats.py + the stats.csv reader.

Covers the metric-row × ecosystem-column assembly, the per-ecosystem
count helpers, and `params.ecosystem_avg_downloads` reading the new
metric-row format (the top-selection denominator — must exclude missing
years, not treat them as zero).
"""

from __future__ import annotations

import csv

from src.value import build_stats as bs


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ── build_stats_rows ─────────────────────────────────────────────────────────

class TestBuildStatsRows:
    def test_matrix_shape_and_metric_rows(self):
        downloads = {"npm": {2021: 100, 2025: 500}, "debian": {2021: 7}}
        counts = {e: {"packages_top": 1, "packages_with_deps": 2, "github_repos": 3}
                  for e in bs.ALL_ECOSYSTEMS}
        rows = bs.build_stats_rows(downloads, counts)
        by_metric = {r["metric"]: r for r in rows}
        from src.common.params import YEARS
        for y in YEARS:
            assert f"downloads_{y}" in by_metric
        for m in ("packages_top", "packages_with_deps", "github_repos"):
            assert m in by_metric
        # every row carries exactly metric + the ecosystem columns
        assert set(rows[0]) == {"metric", *bs.ALL_ECOSYSTEMS}

    def test_cpp_downloads_blank_but_counts_present(self):
        downloads = {"npm": {2021: 100}}
        counts = {e: {"packages_top": 9, "packages_with_deps": 9, "github_repos": 9}
                  for e in bs.ALL_ECOSYSTEMS}
        rows = bs.build_stats_rows(downloads, counts)
        by_metric = {r["metric"]: r for r in rows}
        # cpp is not a download ecosystem → blank download cell …
        assert by_metric["downloads_2021"]["cpp"] == ""
        # … but its package counts are populated
        assert by_metric["packages_top"]["cpp"] == 9

    def test_missing_download_year_is_blank_not_zero(self):
        downloads = {"npm": {2021: 100}}  # only 2021 present
        counts = {e: {} for e in bs.ALL_ECOSYSTEMS}
        rows = bs.build_stats_rows(downloads, counts)
        by_metric = {r["metric"]: r for r in rows}
        assert by_metric["downloads_2021"]["npm"] == 100
        # a year with no datum is blank (missing), not 0
        assert by_metric["downloads_2025"]["npm"] == ""


# ── _count_rows / package_counts ─────────────────────────────────────────────

class TestCounts:
    def test_count_rows_excludes_header(self, tmp_path):
        p = tmp_path / "x.csv"
        _write(p, ["a", "b"], [["1", "2"], ["3", "4"], ["5", "6"]])
        assert bs._count_rows(str(p)) == 3

    def test_count_rows_missing_file_is_blank(self, tmp_path):
        assert bs._count_rows(str(tmp_path / "absent.csv")) == ""

    def test_package_counts_reads_three_files(self, tmp_path):
        eco = tmp_path / "npm"
        eco.mkdir()
        _write(eco / "top-packages.csv", ["package"], [["a"], ["b"]])
        _write(eco / "results.csv", ["package"], [["a"], ["b"], ["c"]])
        _write(eco / "github-repos.csv", ["package", "github_repo"], [["a", "x/y"]])
        c = bs.package_counts("npm", sources_dir=str(tmp_path))
        assert c == {"packages_top": 2, "packages_with_deps": 3, "github_repos": 1}


# ── ecosystem_avg_downloads reads the new metric-row format ───────────────────

class TestAvgDownloadsReader:
    def test_reads_metric_rows_and_skips_zero_years(self, tmp_path, monkeypatch):
        import src.common.params as params
        stats = tmp_path / "stats.csv"
        # homebrew 2021 == 0 (missing) must be excluded from the average.
        _write(stats, ["metric", "npm", "homebrew", "cpp"],
               [["downloads_2021", "100", "0", ""],
                ["downloads_2022", "200", "50", ""],
                ["downloads_2023", "300", "70", ""],
                ["downloads_2024", "400", "90", ""],
                ["downloads_2025", "500", "110", ""],
                ["packages_top", "9", "9", "9"]])
        monkeypatch.setattr(params, "_STATS_PATH", str(stats))
        monkeypatch.setattr(params, "YEARS", [2021, 2022, 2023, 2024, 2025])
        # npm: mean(100..500) = 300
        assert params.ecosystem_avg_downloads("npm") == 300
        # homebrew: 2021 is 0 → excluded; mean(50,70,90,110) = 80
        assert params.ecosystem_avg_downloads("homebrew") == 80
        # cpp: all blank → 0
        assert params.ecosystem_avg_downloads("cpp") == 0
