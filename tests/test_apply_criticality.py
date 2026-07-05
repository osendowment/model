"""Tests for src/value/apply_criticality.py."""

import csv


def _write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _value_row(**over):
    row = {f: "" for f in _FIELDS}
    row.update({"repo": "a/a", "platform": "github", "repo_id": "gh/1",
                "valid": "True", "class": "A"})
    row.update(over)
    return row


from src.value.unify_value_data import FIELDS as _FIELDS  # noqa: E402


def test_apply_joins_by_id_slug_fallback_and_skips_non_github(tmp_path):
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    _write_csv(value, [
        _value_row(repo="a/a", repo_id="gh/1"),                    # id join
        _value_row(repo="b/b", repo_id=""),                        # slug fallback
        _value_row(repo="c/c", repo_id="gl/9", platform="gitlab"),  # non-github
        _value_row(repo="d/d", repo_id="gh/4"),                    # error row: stays blank
    ], _FIELDS)
    _write_csv(crit, [
        {"repo": "a/a", "repo_id": "1", "criticality_score": "0.5", "status": "ok"},
        {"repo": "b/b", "repo_id": "", "criticality_score": "0.4", "status": "ok"},
        {"repo": "d/d", "repo_id": "4", "criticality_score": "", "status": "error"},
    ], ["repo", "repo_id", "criticality_score", "status"])

    from src.value.apply_criticality import apply
    rows = {r["repo"]: r for r in apply(value, crit)}
    assert rows["a/a"]["criticality"] == "0.5"
    assert rows["b/b"]["criticality"] == "0.4"   # matched by slug
    assert rows["c/c"]["criticality"] == ""      # gitlab: tool is github-only
    assert rows["d/d"]["criticality"] == ""      # error is not a measurement


def test_apply_is_idempotent_and_written_file_round_trips(tmp_path):
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    _write_csv(value, [_value_row()], _FIELDS)
    _write_csv(crit, [{"repo": "a/a", "repo_id": "1",
                       "criticality_score": "0.7", "status": "ok"}],
               ["repo", "repo_id", "criticality_score", "status"])

    from src.value.apply_criticality import apply
    apply(value, crit)
    rows = apply(value, crit)  # second run reads its own output
    assert rows[0]["criticality"] == "0.7"
    on_disk = list(csv.DictReader(open(value, encoding="utf-8")))
    assert on_disk[0]["criticality"] == "0.7"


def test_report_flags_blank_valid_class_a_github_rows():
    from src.value.apply_criticality import report
    rows = [
        _value_row(repo="ok/ok", criticality="0.5"),
        _value_row(repo="bad/bad", criticality=""),               # violation
        _value_row(repo="b/b", **{"class": "B"}, criticality=""),  # B: not gated
        _value_row(repo="inv/inv", valid="False", criticality=""),  # invalid: not gated
    ]
    assert report(rows) == ["bad/bad"]
