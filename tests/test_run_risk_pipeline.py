"""Guard: the risk pipeline's fetch steps must cover every score input.

Regression: cyclomatic_max (half the complexity score) comes from the lizard
pass, which was once missing from FETCHERS — the pipeline "fetches missing data
by default" yet could not produce its own scoring input for a newly-scoped repo
(servo/core-foundation-rs was unscoreable). The scc + both lizard passes are now
served by a single sha-pinned fetcher (`fetch_sha_metrics`, one clone per repo).
"""


def test_fetchers_cover_complexity_score_inputs():
    from src.risk.run_risk_pipeline import FETCHERS
    modules = {s.module for s in FETCHERS}
    # complexity score = geomean(loc_eoy_p, cyclomatic_max_p); the unified
    # sha-metrics fetcher produces loc_eoy (scc) AND cyclomatic_max + cognitive
    # (lizard) from one clone.
    assert "src.sources.git.fetch_sha_metrics" in modules
    # semgrep SAST was removed from the pipeline entirely.
    assert "src.sources.github.fetch_semgrep" not in modules


def test_fetchers_cover_concentration_and_workload_score_inputs():
    """The git-clone contributor log is the sole source of the concentration
    score (bf/hhi _5y) AND workload's active_contributors divisor — it must
    be a pipeline step, not a fetcher you have to know to run by hand."""
    from src.risk.run_risk_pipeline import FETCHERS
    modules = {s.module for s in FETCHERS}
    assert "src.sources.git.contributors" in modules
    assert "src.sources.github.fetch_issue_metrics" in modules          # nni_per_ac


def test_sha_metrics_defaults_to_full_scope_incremental():
    """The unified scc+lizard fetcher runs as a pipeline step: no random
    sampling, and a TTL long enough that sha-pinned rows aren't pointlessly
    re-analyzed."""
    from src.sources.git import fetch_sha_metrics as shm
    assert shm.DEFAULT_LIMIT == 0
    assert shm.DEFAULT_TTL_DAYS >= 365
