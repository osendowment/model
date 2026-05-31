"""Risk pipeline runner — dimension builders + aggregation.

By default runs only the cheap projection (the six dimension builders ->
aggregate). Pass --with-fetchers to also run the multi-hour data-collection
fetchers first.

Usage:
    uv run python -m src.pipeline.run_risk_pipeline                 # build + aggregate
    uv run python -m src.pipeline.run_risk_pipeline --with-fetchers # full pipeline
    uv run python -m src.pipeline.run_risk_pipeline --from aggregate
    uv run python -m src.pipeline.run_risk_pipeline --list
"""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

FETCHERS = [
    Step("commits-years", "src.git.commits_years",                fetch=True),
    Step("resolve-head",  "src.git.resolve_head",                 fetch=True),
    Step("contributors",  "src.github.fetch_contributors_metrics", fetch=True),
    Step("issues",        "src.github.fetch_issue_metrics",       fetch=True),
    Step("scc",           "src.git.fetch_scc",                    fetch=True),
    Step("churn",         "src.github.fetch_churn",               fetch=True),
    Step("semgrep",       "src.github.fetch_semgrep",             fetch=True),
    Step("cognitive",     "src.github.fetch_cognitive",           fetch=True),
    Step("cves",          "src.osv.fetch_cves",                   fetch=True),
    Step("scorecard",     "src.openssf.scorecard",                fetch=True),
    Step("depsdev",       "src.depsdev.fetch",                    fetch=True),
    Step("funding-yml",   "src.github.fetch_funding_yml",         fetch=True),
    Step("sponsors",      "src.github.fetch_sponsors",            fetch=True),
    Step("floss-fund",    "src.floss_fund.funding_json",          fetch=True),
]
BUILDERS = [
    Step("concentration", "src.pipeline.risk.build_concentration"),
    Step("complexity",    "src.pipeline.risk.build_complexity"),
    Step("security",      "src.pipeline.risk.build_security"),
    Step("funding-build", "src.pipeline.risk.build_funding"),
    Step("visibility",    "src.pipeline.risk.build_visibility"),
    Step("workload",      "src.pipeline.risk.build_workload"),
    Step("aggregate",     "src.pipeline.risk.aggregate_risk"),
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
