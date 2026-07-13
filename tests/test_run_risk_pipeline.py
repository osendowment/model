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

    The model scores only what it fetches: no fetcher runs whose output no
    score reads. churn (complexity hotspot cols) and fetch_contributors_metrics
    (concentration `_gh_alltime` bus factor/HHI) fed no risk score, were
    GitHub-only, and were computed from caches the pipeline never refreshed —
    both column sets are gone, and neither script is triggered anywhere in
    THIS runner (fetch_contributors_metrics is now a step of the eligibility
    pipeline, the only stage that reads its output; fetch_churn is by-hand)."""
    import src.risk.run_risk_pipeline as mod
    from src.risk.run_risk_pipeline import FETCHERS
    fetch_modules = {s.module for s in FETCHERS}
    # score-forming set, exactly (scc + both lizard passes unified into
    # fetch_sha_metrics — one clone per repo).
    assert fetch_modules == {
        "src.sources.git.commits_years",
        "src.sources.gitlab.commits_years",
        "src.sources.git.resolve_head",
        "src.sources.git.contributors",
        "src.sources.github.fetch_issue_metrics",
        "src.sources.gitlab.fetch_issue_metrics",
        "src.sources.git.fetch_sha_metrics",
        "src.sources.osv.fetch_cves",
        "src.sources.openssf.scorecard",
        "src.sources.depsdev.fetch",
    }
    # The retired audit fetchers must not be triggered by ANY step of the
    # runner (no AUDIT_FETCHERS list to quietly re-enable, either).
    assert not hasattr(mod, "AUDIT_FETCHERS")
    all_modules = {s.module for s in mod.FETCHERS + mod.BUILDERS}
    assert "src.sources.github.fetch_churn" not in all_modules
    assert "src.sources.github.fetch_contributors_metrics" not in all_modules


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


def test_gitlab_commits_years_anchors_gl_repos_and_never_races_the_shared_file():
    """The GitLab per-year SHA pass must be a pipeline step (a gl/ repo entering
    scope otherwise gets no anchor, so sha-metrics leaves complexity + workload
    blank), and must NOT share a pgroup with the other writers of
    commits-years.csv — a pgroup runs concurrently, so sharing one would race
    that file."""
    from src.risk.run_risk_pipeline import FETCHERS
    by_name = {s.label: s for s in FETCHERS}
    gl = by_name["gitlab-commits-years"]
    assert gl.module == "src.sources.gitlab.commits_years"
    # the three steps that write data/sources/git/commits-years.csv
    writers = [by_name["commits-years"], by_name["resolve-head"], gl]
    assert gl.pgroup is None                      # sequential: its own slot
    assert len({w.pgroup for w in writers}) > 1   # not all in one parallel group

    # and it must run BEFORE sha-metrics, which reads those anchors
    order = [s.label for s in FETCHERS]
    assert order.index("gitlab-commits-years") < order.index("sha-metrics")
