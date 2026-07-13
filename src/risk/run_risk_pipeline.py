"""Risk pipeline runner — fetch missing data, then build + aggregate.

By default fetches any MISSING raw data first (incremental — each fetcher
skips data already present in its output files, so only gaps are fetched),
then runs the four dimension builders -> aggregate. Pass --offline to skip
all fetchers and only re-run the builders/aggregate from existing data.

Funding moved to the eligibility stage — its fetchers and builder now live in
src.eligibility.run_eligibility_pipeline.

Scope: FETCHERS holds ONLY the steps whose output feeds a `risk.csv` score
column (concentration / complexity / security / workload → overall
`risk_score`). The model scores nothing it does not fetch: there is no
audit-only fetcher list, and no step runs whose output no score reads. This
keeps a full risk run lean — it fetches exactly what the scores need.

Usage:
    uv run python -m src.risk.run_risk_pipeline                # fetch + build + aggregate
    uv run python -m src.risk.run_risk_pipeline --offline      # build + aggregate only
    uv run python -m src.risk.run_risk_pipeline --from aggregate
    uv run python -m src.risk.run_risk_pipeline --list
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

# Score-forming fetchers — each produces an input to one of the four risk.csv
# dimension scores. Comment marks which score each feeds.
FETCHERS = [
    Step("commits-years", "src.sources.git.commits_years",                fetch=True, pgroup="git-fetch"),  # per-year last_sha anchor (scc/lizard/openssf/workload key off it)
    Step("resolve-head",  "src.sources.git.resolve_head",                 fetch=True, pgroup="git-fetch"),  # current-HEAD sha for the complexity / cyclomatic chain
    # The git-clone contributor log is the ONLY source of the concentration
    # score (bf/hhi _5y) and of workload's per-contributor divisor. It was
    # runnable solely by hand before — a new repo entering scope got blank
    # concentration + workload from a full pipeline run. Incremental: skips
    # repos whose status row is already ok.
    Step("git-contributors", "src.sources.git.contributors",              fetch=True, pgroup="git-fetch"),  # concentration score + workload divisor
    Step("issues",        "src.sources.github.fetch_issue_metrics",       fetch=True, pgroup="git-fetch"),  # workload score
    Step("gitlab-issues",  "src.sources.gitlab.fetch_issue_metrics",       fetch=True, pgroup="git-fetch"),  # workload score for gl/ repos
    # The GitHub-API commits_years cannot anchor a GitLab repo, so gl/ rows get
    # their own per-year SHA pass. Without it a GitLab repo entering scope has no
    # anchor at all, and sha-metrics leaves its complexity + workload blank — the
    # whole scc/lizard chain hangs off these SHAs.
    #
    # Deliberately NOT in the `git-fetch` pgroup: it merge-writes the very same
    # commits-years.csv that `commits-years` / `resolve-head` write, and a pgroup
    # runs its members CONCURRENTLY — sharing one would race the file and drop
    # rows. Its own (sequential) slot sits after that group's barrier and before
    # `metrics`, so the anchors are complete when sha-metrics reads them.
    Step("gitlab-commits-years", "src.sources.gitlab.commits_years",      fetch=True),  # per-year last_sha anchor for gl/ repos
    # One clone per repo yields BOTH scc (loc/complexity → scc.csv) and lizard
    # (cyclomatic + cognitive → lizard.csv). cyclomatic_max is half the
    # complexity score, so a newly-scoped repo cannot be scored without this
    # step. Replaces the former separate scc/cyclomatic steps that each
    # re-cloned the same end-of-year SHA.
    Step("sha-metrics",   "src.sources.git.fetch_sha_metrics",            fetch=True, pgroup="metrics"),  # complexity score: loc_eoy + cyclomatic_max
    Step("cves",          "src.sources.osv.fetch_cves",                   fetch=True, pgroup="metrics"),  # security score: cve_count_5y
    Step("scorecard",     "src.sources.openssf.scorecard",                fetch=True, pgroup="metrics"),  # security score: openssf_score + workload checks
    Step("depsdev",       "src.sources.depsdev.fetch",                    fetch=True, pgroup="metrics"),  # security score: openssf fallback (deps.dev-mirrored)
]

# The model scores ONLY what the pipeline fetches. One GitHub-API fetcher used
# to populate informational columns that no `risk.csv` score reads — churn /
# hotspot in complexity.csv. The pipeline never ran it, so those columns were
# derived from a stale cache and were GitHub-only (blank for most GitLab repos).
# The column set is gone; build_complexity no longer computes it. The script is
# still on disk and runnable by hand if you ever want that data back:
#   uv run python -m src.sources.github.fetch_churn
#
# (src.sources.github.fetch_contributors_metrics — the per-contributor commit
# log behind `bf_maintainer_fundable` — is NOT audit-only: it is a step of the
# eligibility pipeline, which is the only stage that reads it.)
BUILDERS = [
    # Pure transform, no network: flattens the scorecard run's raw JSON into the
    # per-check table build_workload reads (Maintained / CI-Tests / Code-Review).
    # It must re-run whenever `scorecard` does, so it is a step rather than a
    # by-hand script — checks.csv used to age silently behind the JSON.
    Step("openssf-checks", "src.sources.openssf.extract_checks"),
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
