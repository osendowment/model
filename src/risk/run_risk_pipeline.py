"""Risk pipeline runner — fetch missing data, then build + aggregate.

By default fetches any MISSING raw data first (incremental — each fetcher
skips data already present in its output files, so only gaps are fetched),
then runs the four dimension builders -> aggregate. Pass --skip-fetch to skip
all fetchers and only re-run the builders/aggregate from existing data.

Funding moved to the eligibility stage — its fetchers and builder now live in
src.eligibility.run_eligibility_pipeline.

Usage:
    uv run python -m src.risk.run_risk_pipeline                # fetch + build + aggregate
    uv run python -m src.risk.run_risk_pipeline --skip-fetch   # build + aggregate only
    uv run python -m src.risk.run_risk_pipeline --from aggregate
    uv run python -m src.risk.run_risk_pipeline --list
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

FETCHERS = [
    Step("commits-years", "src.sources.git.commits_years",                fetch=True),
    Step("resolve-head",  "src.sources.git.resolve_head",                 fetch=True),
    # The git-clone contributor log is the ONLY source of the concentration
    # score (bf/hhi _5y) and of workload's per-contributor divisor. It was
    # runnable solely by hand before — a new repo entering scope got blank
    # concentration + workload from a full pipeline run. Incremental: skips
    # repos whose status row is already ok.
    Step("git-contributors", "src.sources.git.contributors",              fetch=True),
    Step("contributors",  "src.sources.github.fetch_contributors_metrics", fetch=True),
    Step("issues",        "src.sources.github.fetch_issue_metrics",       fetch=True),
    Step("scc",           "src.sources.git.fetch_scc",                    fetch=True),
    Step("churn",         "src.sources.github.fetch_churn",               fetch=True),
    Step("semgrep",       "src.sources.github.fetch_semgrep",             fetch=True),
    Step("cognitive",     "src.sources.github.fetch_cognitive",           fetch=True),
    # cyclomatic_max is half the complexity score — without this step the
    # pipeline cannot score a newly-scoped repo (cognitive alone is not
    # enough; both write git/lizard.csv but emit disjoint metrics).
    Step("cyclomatic",    "src.sources.github.fetch_advanced_complexity", fetch=True),
    Step("cves",          "src.sources.osv.fetch_cves",                   fetch=True),
    Step("scorecard",     "src.sources.openssf.scorecard",                fetch=True),
    Step("depsdev",       "src.sources.depsdev.fetch",                    fetch=True),
]
BUILDERS = [
    Step("concentration", "src.risk.build_concentration"),
    Step("complexity",    "src.risk.build_complexity"),
    Step("security",      "src.risk.build_security"),
    Step("workload",      "src.risk.build_workload"),
    Step("aggregate",     "src.risk.aggregate_risk"),
]


def main() -> int:
    parser = build_parser("risk pipeline runner")
    args = parser.parse_args()
    steps = FETCHERS + BUILDERS
    return run_pipeline(steps, args)


if __name__ == "__main__":
    raise SystemExit(main())
