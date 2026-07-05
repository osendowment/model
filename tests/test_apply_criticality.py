"""Tests for src/value/apply_criticality.py."""

import csv

from src.common.params import (
    VALUE_SCORE_CENTRALITY_WEIGHT,
    VALUE_SCORE_CRIT_WEIGHT,
    VALUE_SCORE_ECO_CRIT_WEIGHT,
    VALUE_SCORE_MIN_COMPONENTS,
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


def test_shipped_weights_and_pro_rata_blend():
    """Default weights are the criticality-dominant 0.6/0.2/0.2 and `score` is the
    pro-rata blend of the present components, renormalized by their weight sum."""
    from src.value.apply_criticality import compute_score
    assert (VALUE_SCORE_CRIT_WEIGHT, VALUE_SCORE_ECO_CRIT_WEIGHT,
            VALUE_SCORE_CENTRALITY_WEIGHT) == (0.6, 0.2, 0.2)
    assert VALUE_SCORE_MIN_COMPONENTS == 2
    # all three present: 0.6*50 + 0.2*100 + 0.2*40 = 58.0
    assert compute_score(0.5, 1.0, 40.0) == 58.0
    # eco_crit absent → openssf + centrality renormalize to 0.75/0.25 → 47.5
    assert compute_score(0.5, None, 40.0) == 47.5
    # openssf absent (a GitLab repo) → eco_crit + centrality → 0.5/0.5 → 70.0
    assert compute_score(None, 1.0, 40.0) == 70.0
    # eco_crit=0 is a PRESENT component (contributes 0), not absent:
    # 0.6*50 + 0.2*0 + 0.2*40 = 38.0
    assert compute_score(0.5, 0.0, 40.0) == 38.0


def test_compute_score_requires_min_components():
    """Fewer than VALUE_SCORE_MIN_COMPONENTS present → blank score (None)."""
    from src.value.apply_criticality import compute_score
    assert compute_score(None, None, 40.0) is None   # only centrality
    assert compute_score(0.5, None, None) is None     # only openssf
    assert compute_score(None, None, None) is None     # nothing


def test_apply_joins_openssf_eco_crit_and_scores_gitlab(tmp_path):
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    eco = tmp_path / "eco.csv"
    _write_csv(value, [
        _value_row(repo="a/a", repo_id="gh/1", top_eco_pct="40"),        # all 3
        _value_row(repo="b/b", repo_id="", top_eco_pct="40"),            # openssf via slug, no eco
        _value_row(repo="g/g", repo_id="gl/9", platform="gitlab",
                   git_url="https://gitlab.gnome.org/g/g.git",
                   top_eco_pct="40"),                                     # gitlab: eco + centrality
        _value_row(repo="d/d", repo_id="gh/4", top_eco_pct="40"),        # openssf error + no eco
    ], _FIELDS)
    _write_csv(crit, [
        {"repo": "a/a", "repo_id": "gh/1", "criticality_score": "0.5", "status": "ok"},
        {"repo": "b/b", "repo_id": "", "criticality_score": "0.5", "status": "ok"},
        {"repo": "d/d", "repo_id": "gh/4", "criticality_score": "", "status": "error"},
    ], ["repo", "repo_id", "criticality_score", "status"])
    _write_csv(eco, [
        {"repo_id": "gh/1", "repository_url": "https://github.com/a/a",
         "ok": "True", "critical": "True"},
        {"repo_id": "gl/9", "repository_url": "https://gitlab.gnome.org/g/g",
         "ok": "True", "critical": "True"},
    ], ["repo_id", "repository_url", "ok", "critical"])

    from src.value.apply_criticality import apply
    rows = {r["repo"]: r for r in apply(value, crit, eco)}
    # a/a: 0.6*(0.5*100) + 0.2*(1*100) + 0.2*40 = 58.00
    assert rows["a/a"]["openssf_crit"] == "0.5"
    assert rows["a/a"]["eco_crit"] == "1"
    assert rows["a/a"]["score"] == "58.00"
    # b/b: openssf matched by slug, no eco row → 2 comps 0.75/0.25 → 47.50
    assert rows["b/b"]["openssf_crit"] == "0.5"
    assert rows["b/b"]["eco_crit"] == ""
    assert rows["b/b"]["score"] == "47.50"
    # g/g: gitlab (no openssf), eco=1 + top 40 → 0.5/0.5 → 70.00 (newly scored)
    assert rows["g/g"]["openssf_crit"] == ""
    assert rows["g/g"]["eco_crit"] == "1"
    assert rows["g/g"]["score"] == "70.00"
    # d/d: openssf error (blank) + no eco → only top_eco_pct → 1 comp → blank
    assert rows["d/d"]["openssf_crit"] == ""
    assert rows["d/d"]["eco_crit"] == ""
    assert rows["d/d"]["score"] == ""


def test_eco_crit_url_fallback_and_zero_flag(tmp_path):
    """eco_crit joins by normalized git URL when repo_id doesn't match, and a
    resolved-but-not-critical row yields "0" (a present component, not absent)."""
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    eco = tmp_path / "eco.csv"
    _write_csv(value, [
        _value_row(repo="u/u", repo_id="gh/nomatch",
                   git_url="https://github.com/u/u.git", top_eco_pct="40"),
    ], _FIELDS)
    _write_csv(crit, [
        {"repo": "u/u", "repo_id": "gh/nomatch", "criticality_score": "0.5",
         "status": "ok"},
    ], ["repo", "repo_id", "criticality_score", "status"])
    _write_csv(eco, [
        # id differs; only the URL matches → fallback join, critical=False → "0"
        {"repo_id": "gh/other", "repository_url": "https://github.com/u/u",
         "ok": "True", "critical": "False"},
    ], ["repo_id", "repository_url", "ok", "critical"])

    from src.value.apply_criticality import apply
    row = apply(value, crit, eco)[0]
    assert row["eco_crit"] == "0"                      # URL fallback hit
    # 0.6*(0.5*100) + 0.2*(0*100) + 0.2*40 = 38.00
    assert row["score"] == "38.00"


def test_eco_crit_ok_false_row_stays_blank_not_zero(tmp_path):
    """An eco_crit fetch that didn't resolve (ok=False) must leave the column
    blank (unchecked), never a real 0 — the auditability invariant."""
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    eco = tmp_path / "eco.csv"
    _write_csv(value, [
        _value_row(repo="a/a", repo_id="gh/1", top_eco_pct="40"),
    ], _FIELDS)
    _write_csv(crit, [
        {"repo": "a/a", "repo_id": "gh/1", "criticality_score": "0.5",
         "status": "ok"},
    ], ["repo", "repo_id", "criticality_score", "status"])
    _write_csv(eco, [
        {"repo_id": "gh/1", "repository_url": "https://github.com/a/a",
         "ok": "False", "critical": ""},
    ], ["repo_id", "repository_url", "ok", "critical"])

    from src.value.apply_criticality import apply
    row = apply(value, crit, eco)[0]
    assert row["eco_crit"] == ""                       # unresolved → blank
    # eco absent → openssf + centrality only → 0.75/0.25 → 47.50
    assert row["score"] == "47.50"


def test_eco_crit_checked_but_blank_flag_is_empty_not_zero(tmp_path):
    """A resolved fetch (ok=True) whose registry omitted the `critical` field
    yields blank eco_crit (unknown), never 0 — only an explicit `critical=False`
    is 0. Common for spack/debian cpp packages."""
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    eco = tmp_path / "eco.csv"
    _write_csv(value, [
        _value_row(repo="lib/glib", repo_id="gl/9", platform="gitlab",
                   git_url="https://gitlab.gnome.org/lib/glib.git", top_eco_pct="46"),
    ], _FIELDS)
    _write_csv(crit, [], ["repo", "repo_id", "criticality_score", "status"])
    _write_csv(eco, [
        {"repo_id": "gl/9", "repository_url": "https://gitlab.gnome.org/lib/glib",
         "ok": "True", "critical": ""},          # resolved, no flag → unknown
    ], ["repo_id", "repository_url", "ok", "critical"])

    from src.value.apply_criticality import apply
    row = apply(value, crit, eco)[0]
    assert row["eco_crit"] == ""                       # checked-but-blank → blank
    # no openssf (gitlab) + eco blank → only top_eco_pct → 1 component → no score
    assert row["score"] == ""


def test_apply_is_idempotent_and_written_file_round_trips(tmp_path):
    value = tmp_path / "value.csv"
    crit = tmp_path / "criticality.csv"
    eco = tmp_path / "eco.csv"
    _write_csv(value, [_value_row(top_eco_pct="50")], _FIELDS)
    _write_csv(crit, [{"repo": "a/a", "repo_id": "gh/1",
                       "criticality_score": "0.7", "status": "ok"}],
               ["repo", "repo_id", "criticality_score", "status"])
    _write_csv(eco, [{"repo_id": "gh/1", "repository_url": "https://github.com/a/a",
                      "ok": "True", "critical": "True"}],
               ["repo_id", "repository_url", "ok", "critical"])

    from src.value.apply_criticality import apply
    apply(value, crit, eco)
    rows = apply(value, crit, eco)  # second run reads its own output
    # 0.6*70 + 0.2*100 + 0.2*50 = 72.00
    assert rows[0]["openssf_crit"] == "0.7"
    assert rows[0]["eco_crit"] == "1"
    assert rows[0]["score"] == "72.00"
    on_disk = list(csv.DictReader(open(value, encoding="utf-8")))
    assert on_disk[0]["openssf_crit"] == "0.7"
    assert on_disk[0]["eco_crit"] == "1"
    assert on_disk[0]["score"] == "72.00"


def test_report_flags_blank_valid_class_a_github_rows():
    from src.value.apply_criticality import report
    rows = [
        _value_row(repo="ok/ok", openssf_crit="0.5", score="40.00"),
        _value_row(repo="bad/bad", openssf_crit=""),               # violation
        _value_row(repo="b/b", **{"class": "B"}, openssf_crit=""),  # B: not gated
        _value_row(repo="inv/inv", git_valid="False", openssf_crit=""),  # invalid: not gated
    ]
    assert report(rows) == ["bad/bad"]
