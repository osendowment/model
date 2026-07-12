"""Tests for src/build_results.py — the final cross-stage rollup (all top repos)."""
import csv

from src import build_results as br


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _patch(monkeypatch, elig, val, risk, repos=None):
    monkeypatch.setattr(br, "ELIGIBILITY_FILE", elig)
    monkeypatch.setattr(br, "VALUE_FILE", val)
    monkeypatch.setattr(br, "RISK_FILE", risk)
    if repos is not None:
        monkeypatch.setattr(br, "GITHUB_REPOS_FILE", repos)


def test_includes_every_top_repo_and_joins_on_repo_id(tmp_path, monkeypatch):
    """Every eligibility row survives (ineligible NOT dropped); value/risk/
    language all join on the stable `repo_id`."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    repos = tmp_path / "repos.csv"
    _write(elig, ["repo", "repo_id", "oss", "intent", "nonprofit", "active", "eligible"],
           [["a/keep", "gh/1", "True", "True", "True", "True", "True"],
            ["b/drop", "gh/2", "True", "False", "True", "True", "False"]])
    _write(val, ["repo", "repo_id", "platform", "top_eco", "top_eco_pkg", "openssf_crit",
                 "eco_crit", "top_eco_pct", "pr_score", "value_score"],
           [["a/keep", "gh/1", "github", "npm", "left-pad", "0.71", "100", "55.5", "41.379", "72.3"],
            ["b/drop", "gh/2", "github", "pypi", "requests", "0.2", "0", "10.0", "5.0", "8.1"]])
    _write(risk, ["repo", "repo_id", "concentration", "complexity", "security",
                  "workload", "risk_score"],
           [["a/keep", "gh/1", "10", "20", "30", "40", "80"],
            ["b/drop", "gh/2", "1", "2", "3", "4", "20"]])
    _write(repos, ["repo_id", "language"], [["gh/1", "TypeScript"], ["gh/2", "Python"]])
    _patch(monkeypatch, elig, val, risk, repos)

    rows = br.build()
    assert {r["repo"] for r in rows} == {"a/keep", "b/drop"}   # nothing dropped
    keep = next(r for r in rows if r["repo"] == "a/keep")
    assert keep["repo_id"] == "gh/1"
    assert keep["language"] == "typescript"        # lowercased
    assert keep["platform"] == "github"
    assert keep["ecosystem"] == "npm"
    assert keep["top_eco_pkg"] == "left-pad"   # verbatim pass-through
    assert keep["eco_crit"] == "100"        # a 0/100 flag, not a score -- left raw
    assert keep["openssf_crit"] == "0.71"
    assert keep["top_eco_pct"] == "55.50"   # rounded to 2dp
    assert keep["pr_score"] == "41.38"      # rounded to 2dp
    assert keep["value_score"] == "72.30"
    assert keep["concentration"] == "10.00" and keep["workload"] == "40.00"
    assert keep["risk_score"] == "80.00"
    assert keep["eligible"] == "True"
    assert list(keep.keys()) == br.FIELDS
    assert br.FIELDS.index("platform") == br.FIELDS.index("language") + 1
    assert br.FIELDS.index("top_eco_pkg") == br.FIELDS.index("ecosystem") + 1
    assert br.FIELDS.index("top_eco_pct") == br.FIELDS.index("top_eco_pkg") + 1
    assert br.FIELDS.index("pr_score") == br.FIELDS.index("top_eco_pct") + 1
    assert br.FIELDS.index("score") == br.FIELDS.index("risk_score") + 1


def test_score_geometric_mean_and_priority_dense_rank_over_eligible(tmp_path, monkeypatch):
    """score = sqrt(value_score*risk_score), unnormalized (same 0-100 scale as
    its inputs); priority = dense rank by score desc, eligible only."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "eligible"],
           [["a/top", "gh/1", "True"], ["b/mid", "gh/2", "True"],
            ["c/inelig", "gh/3", "False"]])
    _write(val, ["repo", "repo_id", "top_eco", "value_score"],
           [["a/top", "gh/1", "npm", "10"], ["b/mid", "gh/2", "npm", "5"],
            ["c/inelig", "gh/3", "npm", "20"]])
    _write(risk, ["repo", "repo_id", "risk_score"],
           [["a/top", "gh/1", "10"], ["b/mid", "gh/2", "10"],
            ["c/inelig", "gh/3", "10"]])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    by = {r["repo"]: r for r in rows}
    # raw products: a=100, b=50, c=200 → sqrt → 10.00 / 7.07 / 14.14 (no rescale)
    assert by["c/inelig"]["score"] == "14.14"
    assert by["a/top"]["score"] == "10.00"
    assert by["b/mid"]["score"] == "7.07"
    # priority only over eligible rows: a (10) rank1, b (7.07) rank2; c blank.
    assert by["a/top"]["priority"] == "1"
    assert by["b/mid"]["priority"] == "2"
    assert by["c/inelig"]["priority"] == ""
    # row order: scored desc → c, a, b
    assert [r["repo"] for r in rows] == ["c/inelig", "a/top", "b/mid"]


def test_missing_risk_leaves_score_blank(tmp_path, monkeypatch):
    """An eligible repo with no risk.csv row keeps blank risk_score AND blank
    score (honest, not a fake 0) yet still appears in the table."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "eligible"], [["x/norisk", "gh/9", "True"]])
    _write(val, ["repo", "repo_id", "top_eco", "value_score"],
           [["x/norisk", "gh/9", "crates", "63.7"]])
    _write(risk, ["repo", "repo_id", "risk_score"], [])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    assert len(rows) == 1
    assert rows[0]["risk_score"] == ""
    assert rows[0]["value_score"] == "63.70"
    assert rows[0]["score"] == ""
    assert rows[0]["priority"] == ""


def test_score_columns_rounded_to_two_decimals(tmp_path, monkeypatch):
    """openssf_crit / top_eco_pct / value_score / risk components / risk_score
    all round to 2dp regardless of upstream precision; eco_crit (a 0/100 flag,
    not a score) is left as its raw upstream value; score itself is computed
    from the FULL-PRECISION upstream values, unaffected by the display
    rounding of value_score/risk_score."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "eligible"], [["o/r", "gh/1", "True"]])
    _write(val, ["repo", "repo_id", "top_eco", "openssf_crit", "eco_crit",
                 "top_eco_pct", "value_score"],
           [["o/r", "gh/1", "npm", "0.604553", "100", "39.35893", "64.145678"]])
    _write(risk, ["repo", "repo_id", "concentration", "complexity", "security",
                  "workload", "risk_score"],
           [["o/r", "gh/1", "95.001", "94", "75.999", "88", "88.4321"]])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    row = rows[0]
    assert row["openssf_crit"] == "0.60"
    assert row["eco_crit"] == "100"
    assert row["top_eco_pct"] == "39.36"
    assert row["value_score"] == "64.15"
    assert row["concentration"] == "95.00"
    assert row["complexity"] == "94.00"
    assert row["security"] == "76.00"
    assert row["workload"] == "88.00"
    assert row["risk_score"] == "88.43"
    assert row["score"] == "75.32"  # sqrt of the FULL-precision product, then 2dp


def test_missing_language_row_leaves_blank(tmp_path, monkeypatch):
    """A repo absent from github/repos.csv (e.g. GitLab) keeps a blank language
    rather than dropping the row."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    repos = tmp_path / "repos.csv"
    _write(elig, ["repo", "repo_id", "eligible"], [["gl/thing", "gl/x-1", "True"]])
    _write(val, ["repo", "repo_id", "top_eco", "value_score"],
           [["gl/thing", "gl/x-1", "npm", "5"]])
    _write(risk, ["repo", "repo_id", "risk_score"], [["gl/thing", "gl/x-1", "5"]])
    _write(repos, ["repo_id", "language"], [])
    _patch(monkeypatch, elig, val, risk, repos)

    rows = br.build()
    assert rows[0]["language"] == ""


def test_priority_breaks_2dp_score_ties_on_full_precision(tmp_path, monkeypatch):
    """Two repos whose scores round to the same 2dp value must rank by the
    full-precision value*risk product, not by the repo-name tie-break.
    z/higher has the (slightly) larger raw product but sorts LAST by name —
    it must still take the better priority."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "oss", "intent", "nonprofit", "active", "eligible"],
           [["a/lower", "gh/1", "True", "True", "True", "True", "True"],
            ["z/higher", "gh/2", "True", "True", "True", "True", "True"]])
    # sqrt(50.0 * 72.0) = 60.0000, sqrt(50.005 * 72.0) = 60.0030 — both display
    # as 60.00, but z/higher's raw product is larger.
    _write(val, ["repo", "repo_id", "platform", "top_eco", "top_eco_pkg", "openssf_crit",
                 "eco_crit", "top_eco_pct", "pr_score", "value_score"],
           [["a/lower", "gh/1", "github", "npm", "a", "1", "1", "1", "1", "50.0"],
            ["z/higher", "gh/2", "github", "npm", "z", "1", "1", "1", "1", "50.005"]])
    _write(risk, ["repo", "repo_id", "concentration", "complexity", "security",
                  "workload", "risk_score"],
           [["a/lower", "gh/1", "1", "1", "1", "1", "72.0"],
            ["z/higher", "gh/2", "1", "1", "1", "1", "72.0"]])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    by = {r["repo"]: r for r in rows}
    assert by["a/lower"]["score"] == by["z/higher"]["score"] == "60.00"  # 2dp tie
    assert by["z/higher"]["priority"] == "1"                             # raw product wins
    assert by["a/lower"]["priority"] == "2"
