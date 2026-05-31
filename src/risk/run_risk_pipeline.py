"""Risk pipeline runner — dimension builders + aggregation.

By default runs only the cheap projection (the six dimension builders ->
aggregate). Pass --with-fetchers to also run the multi-hour data-collection
fetchers first.

Usage:
    uv run python -m src.risk.run_risk_pipeline                 # build + aggregate
    uv run python -m src.risk.run_risk_pipeline --with-fetchers # full pipeline
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
    Step("sponsors",      "src.sources.github.fetch_sponsors",            fetch=True),
    Step("floss-fund",    "src.sources.floss_fund.funding_json",          fetch=True),
    Step("opencollective", "src.sources.opencollective.fetch_budgets",    fetch=True),
]
BUILDERS = [
    Step("concentration", "src.risk.build_concentration"),
    Step("complexity",    "src.risk.build_complexity"),
    Step("security",      "src.risk.build_security"),
    Step("funding-build", "src.risk.build_funding"),
    Step("visibility",    "src.risk.build_visibility"),
    Step("workload",      "src.risk.build_workload"),
    Step("aggregate",     "src.risk.aggregate_risk"),
]


def main() -> int:
    parser = build_parser("risk pipeline runner")
    parser.add_argument("--with-fetchers", action="store_true",
                        help="Also run the multi-hour data-collection fetchers first")
    args = parser.parse_args()
    steps = (FETCHERS + BUILDERS) if args.with_fetchers else BUILDERS
    return run_pipeline(steps, args)


if __name__ == "__main__":
    raise SystemExit(main())
