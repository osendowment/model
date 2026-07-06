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


def test_pipeline_runs_only_score_forming_fetchers():
    """FETCHERS holds exactly the steps that feed a risk.csv score column.
    Audit-only fetchers (informational columns that never enter a score) live
    in AUDIT_FETCHERS and are NOT run by the pipeline — regression for the
    pipeline silently re-fetching churn/github-contributors every run when
    nothing downstream scores their output."""
    from src.risk.run_risk_pipeline import FETCHERS, AUDIT_FETCHERS
    fetch_modules = {s.module for s in FETCHERS}
    audit_modules = {s.module for s in AUDIT_FETCHERS}
    # score-forming set, exactly (scc + both lizard passes unified into
    # fetch_sha_metrics — one clone per repo).
    assert fetch_modules == {
        "src.sources.git.commits_years",
        "src.sources.git.resolve_head",
        "src.sources.git.contributors",
        "src.sources.github.fetch_issue_metrics",
        "src.sources.git.fetch_sha_metrics",
        "src.sources.osv.fetch_cves",
        "src.sources.openssf.scorecard",
        "src.sources.depsdev.fetch",
    }
    # audit-only fetchers exist on disk but are excluded from the pipeline.
    # (cognitive folded into sha-metrics; semgrep removed entirely.)
    assert audit_modules == {
        "src.sources.github.fetch_churn",
        "src.sources.github.fetch_contributors_metrics",
    }
    assert not (fetch_modules & audit_modules)   # disjoint


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


def test_risk_fetchers_share_the_365_day_freshness_gate():
    """The TTL-gated risk fetchers use a 365-day freshness window, so an
    incremental run gap-fills newly-scoped (e.g. archived) repos instead of
    re-fetching the whole scope every time a month elapses. Regression for
    churn/cves/depsdev silently full-refetching ~900 repos."""
    from src.sources.github import fetch_churn
    from src.sources.osv import fetch_cves
    from src.sources.depsdev import fetch as depsdev
    assert fetch_churn.DEFAULT_TTL_DAYS >= 365
    assert fetch_cves.TTL_DAYS_DEFAULT >= 365
    assert depsdev.TTL_DAYS_DEFAULT >= 365
