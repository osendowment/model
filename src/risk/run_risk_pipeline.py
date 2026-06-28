"""Risk pipeline runner — fetch missing data, then build + aggregate.

By default fetches any MISSING raw data first (incremental — each fetcher
skips data already present in its output files, so only gaps are fetched),
then runs the six dimension builders -> aggregate. Pass --skip-fetch to skip
all fetchers and only re-run the builders/aggregate from existing data.

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
    Step("contributors",  "src.sources.github.fetch_contributors_metrics", fetch=True),
    Step("issues",        "src.sources.github.fetch_issue_metrics",       fetch=True),
    Step("scc",           "src.sources.git.fetch_scc",                    fetch=True),
    Step("churn",         "src.sources.github.fetch_churn",               fetch=True),
    Step("semgrep",       "src.sources.github.fetch_semgrep",             fetch=True),
    Step("cognitive",     "src.sources.github.fetch_cognitive",           fetch=True),
    Step("cves",          "src.sources.osv.fetch_cves",                   fetch=True),
    Step("scorecard",     "src.sources.openssf.scorecard",                fetch=True),
    Step("depsdev",       "src.sources.depsdev.fetch",                    fetch=True),
    Step("funding-yml",   "src.sources.github.fetch_funding_yml",         fetch=True),
    Step("npm-funding",   "src.sources.npm.fetch_funding",                fetch=True),
    Step("pypi-funding",  "src.sources.pypi.fetch_funding",               fetch=True),
    Step("sponsors",      "src.sources.github.fetch_sponsors",            fetch=True),
    Step("floss-fund",    "src.sources.floss_fund.funding_json",          fetch=True),
    Step("oc-collectives", "src.sources.opencollective.fetch_collectives", fetch=True),
    Step("opencollective", "src.sources.opencollective.fetch_budgets",    fetch=True),
]
BUILDERS = [
    Step("concentration", "src.risk.build_concentration"),
    Step("complexity",    "src.risk.build_complexity"),
    Step("security",      "src.risk.build_security"),
    Step("funding-build", "src.risk.build_funding"),
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
