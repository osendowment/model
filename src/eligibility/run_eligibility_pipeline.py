"""Eligibility pipeline runner — fetch missing data, then build + aggregate.

Stage 3 of the pipeline (Value → Risk → Eligibility). Takes the top repos
(valid class-A, archived included) and answers: which of them are eligible
for funding? Four checks — OSI-approved license (`oss`), funding intent
(`intent`), not company-backed (`nonprofit`), not EOL/archived (`active`).

By default fetches any MISSING raw data first (incremental — each fetcher
skips data already present / within its TTL, so only gaps are fetched),
then runs the three dimension builders -> aggregate. Pass --skip-fetch to
skip all fetchers and only re-run the builders/aggregate from existing data.

Usage:
    uv run python -m src.eligibility.run_eligibility_pipeline                # fetch + build
    uv run python -m src.eligibility.run_eligibility_pipeline --skip-fetch   # build only
    uv run python -m src.eligibility.run_eligibility_pipeline --from aggregate
    uv run python -m src.eligibility.run_eligibility_pipeline --list
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

FETCHERS = [
    # repo state — archived flag + GitHub license fallback
    Step("repo-owner",    "src.sources.github.fetch_repo_owner_data",     fetch=True),
    # license signals (homebrew before cpp — cpp joins homebrew's licenses)
    Step("osi",           "src.sources.osi.fetch_licenses",               fetch=True),
    Step("npm-lic",       "src.sources.npm.fetch_licenses",               fetch=True),
    Step("pypi-lic",      "src.sources.pypi.fetch_licenses",              fetch=True),
    Step("crates-lic",    "src.sources.crates.fetch_licenses",            fetch=True),
    Step("homebrew-lic",  "src.sources.homebrew.fetch_licenses",          fetch=True),
    Step("cpp-lic",       "src.sources.cpp.fetch_licenses",               fetch=True),
    # registry EOL signals — advisory inputs to the manual `eol` override
    # in data/eligibility/overrides.csv (see build_active)
    Step("npm-eol",       "src.sources.npm.check_eol",                    fetch=True),
    Step("pypi-eol",      "src.sources.pypi.check_eol",                   fetch=True),
    Step("crates-eol",    "src.sources.crates.check_eol",                 fetch=True),
    Step("cpp-eol",       "src.sources.cpp.check_eol",                    fetch=True),
    # funding-intent signals (moved here from the risk pipeline)
    Step("funding-yml",   "src.sources.github.fetch_funding_yml",         fetch=True),
    Step("npm-funding",   "src.sources.npm.fetch_funding",                fetch=True),
    Step("pypi-funding",  "src.sources.pypi.fetch_funding",               fetch=True),
    Step("sponsors",      "src.sources.github.fetch_sponsors",            fetch=True),
    Step("floss-fund",    "src.sources.floss_fund.funding_json",          fetch=True),
    Step("oc-collectives", "src.sources.opencollective.fetch_collectives", fetch=True),
    Step("opencollective", "src.sources.opencollective.fetch_budgets",    fetch=True),
    # FOSS-foundation rosters → host-by-repo (nonprofit / host signal)
    Step("apache",        "src.sources.funding.apache",                   fetch=True),
    Step("cncf",          "src.sources.funding.cncf",                     fetch=True),
    Step("eclipse",       "src.sources.funding.eclipse",                  fetch=True),
    Step("fsf",           "src.sources.funding.fsf",                      fetch=True),
    Step("gnu",           "src.sources.funding.gnu",                      fetch=True),
    Step("lf",            "src.sources.funding.lf",                       fetch=True),
    Step("numfocus",      "src.sources.funding.numfocus",                 fetch=True),
    Step("openjs",        "src.sources.funding.openjs",                   fetch=True),
    Step("psf",           "src.sources.funding.psf",                      fetch=True),
    Step("sfc",           "src.sources.funding.sfc",                      fetch=True),
    Step("match-hosts",   "src.sources.funding.match_repos",              fetch=True),
]
BUILDERS = [
    Step("licenses",      "src.eligibility.build_licenses"),
    Step("active",        "src.eligibility.build_active"),
    Step("funding-build", "src.eligibility.build_funding"),
    Step("aggregate",     "src.eligibility.build_eligibility"),
    # Terminal cross-stage rollup: eligible repos + value_score + risk_score.
    Step("results",       "src.build_results"),
]


def main() -> int:
    parser = build_parser("eligibility pipeline runner")
    args = parser.parse_args()
    steps = FETCHERS + BUILDERS
    return run_pipeline(steps, args)


if __name__ == "__main__":
    raise SystemExit(main())
