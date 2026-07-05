"""Guard: the risk pipeline's fetch steps must cover every score input.

Regression: cyclomatic_max (half the complexity score) comes from
fetch_advanced_complexity, which was missing from FETCHERS — the pipeline
"fetches missing data by default" yet could not produce its own scoring
input for a newly-scoped repo (servo/core-foundation-rs was unscoreable).
"""


def test_fetchers_cover_complexity_score_inputs():
    from src.risk.run_risk_pipeline import FETCHERS
    modules = {s.module for s in FETCHERS}
    # complexity score = geomean(loc_eoy_p, cyclomatic_max_p)
    assert "src.sources.git.fetch_scc" in modules                       # loc_eoy
    assert "src.sources.github.fetch_advanced_complexity" in modules   # cyclomatic_max
    assert "src.sources.github.fetch_cognitive" in modules             # cognitive audit cols


def test_fetchers_cover_concentration_and_workload_score_inputs():
    """The git-clone contributor log is the sole source of the concentration
    score (bf/hhi _5y) AND workload's active_contributors divisor — it must
    be a pipeline step, not a fetcher you have to know to run by hand."""
    from src.risk.run_risk_pipeline import FETCHERS
    modules = {s.module for s in FETCHERS}
    assert "src.sources.git.contributors" in modules
    assert "src.sources.github.fetch_issue_metrics" in modules          # nni_per_ac


def test_lizard_fetchers_default_to_full_scope_incremental():
    """Both lizard fetchers run as pipeline steps: no random sampling, and a
    TTL long enough that sha-pinned rows aren't pointlessly re-analyzed."""
    from src.sources.github import fetch_advanced_complexity as cyclo
    from src.sources.github import fetch_cognitive as cog
    assert cyclo.DEFAULT_LIMIT == 0
    assert cog.DEFAULT_LIMIT == 0
    assert cyclo.DEFAULT_TTL_DAYS >= 365
    assert cog.DEFAULT_TTL_DAYS >= 365
