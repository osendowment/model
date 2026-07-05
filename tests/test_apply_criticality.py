"""Tests for src/value/apply_criticality.py."""

import csv

from src.common.params import (
    VALUE_SCORE_CENTRALITY_WEIGHT,
    VALUE_SCORE_CRIT_WEIGHT,
)
from src.value.unify_value_data import FIELDS as _FIELDS


def _write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _value_row(**over):
    row = {f: "" for f in _FIELDS}
    row.update({"repo": "a/a", "platform": "github", "repo_id": "gh/1",
                "git_valid": "True", "class": "A"})
    row.update(over)
    return row


def test_shipped_weights_and_score_formula():
    """Default weights are the criticality-dominant 0.8/0.2, and `score` is the
    raw 0–100 blend crit·(openssf·100) + centrality·top_eco_pct."""
    from src.value.apply_criticality import compute_score
    assert (VALUE_SCORE_CRIT_WEIGHT, VALUE_SCORE_CENTRALITY_WEIGHT) == (0.8, 0.2)
    assert compute_score(0.5, 40.0) == 48.0   # 0.8*50 + 0.2*40
    assert compute_score(1.0, 100.0) == 100.0
    assert compute_score(0.0, 0.0) == 0.0


def test_apply_joins_by_id_slug_fallback_and_skips_non_github(tmp_path):
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    _write_csv(value, [
        _value_row(repo="a/a", repo_id="gh/1", top_eco_pct="40"),   # id join + score
        _value_row(repo="b/b", repo_id=""),                        # slug fallback
        _value_row(repo="c/c", repo_id="gl/9", platform="gitlab"),  # non-github
        _value_row(repo="d/d", repo_id="gh/4"),                    # error row: stays blank
    ], _FIELDS)
    _write_csv(crit, [
        {"repo": "a/a", "repo_id": "gh/1", "criticality_score": "0.5", "status": "ok"},
        {"repo": "b/b", "repo_id": "", "criticality_score": "0.4", "status": "ok"},
        {"repo": "d/d", "repo_id": "gh/4", "criticality_score": "", "status": "error"},
    ], ["repo", "repo_id", "criticality_score", "status"])

    from src.value.apply_criticality import apply
    rows = {r["repo"]: r for r in apply(value, crit)}
    assert rows["a/a"]["openssf_crit"] == "0.5"
    assert rows["a/a"]["score"] == "48.00"       # 0.8*(0.5*100) + 0.2*40
    assert rows["b/b"]["openssf_crit"] == "0.4"  # matched by slug
    assert rows["b/b"]["score"] == "32.00"       # top_eco_pct blank -> 0
    assert rows["c/c"]["openssf_crit"] == ""     # gitlab: tool is github-only
    assert rows["c/c"]["score"] == ""
    assert rows["d/d"]["openssf_crit"] == ""     # error is not a measurement
    assert rows["d/d"]["score"] == ""


def test_apply_is_idempotent_and_written_file_round_trips(tmp_path):
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    _write_csv(value, [_value_row(top_eco_pct="50")], _FIELDS)
    _write_csv(crit, [{"repo": "a/a", "repo_id": "gh/1",
                       "criticality_score": "0.7", "status": "ok"}],
               ["repo", "repo_id", "criticality_score", "status"])

    from src.value.apply_criticality import apply
    apply(value, crit)
    rows = apply(value, crit)  # second run reads its own output
    assert rows[0]["openssf_crit"] == "0.7"
    assert rows[0]["score"] == "66.00"           # 0.8*70 + 0.2*50
    on_disk = list(csv.DictReader(open(value, encoding="utf-8")))
    assert on_disk[0]["openssf_crit"] == "0.7"
    assert on_disk[0]["score"] == "66.00"


def test_report_flags_blank_valid_class_a_github_rows():
    from src.value.apply_criticality import report
    rows = [
        _value_row(repo="ok/ok", openssf_crit="0.5", score="40.00"),
        _value_row(repo="bad/bad", openssf_crit=""),               # violation
        _value_row(repo="b/b", **{"class": "B"}, openssf_crit=""),  # B: not gated
        _value_row(repo="inv/inv", git_valid="False", openssf_crit=""),  # invalid: not gated
    ]
    assert report(rows) == ["bad/bad"]
