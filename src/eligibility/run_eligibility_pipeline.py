"""Eligibility pipeline runner — fetch missing data, then build + aggregate.

Stage 3 of the pipeline (Value → Risk → Eligibility). Takes the top repos
(valid class-A, archived included) and answers: which of them are eligible
for funding? Four checks — OSI-approved license (`oss`), funding intent
(`intent`), not company-backed (`nonprofit`), not EOL/archived (`active`).

By default fetches any MISSING raw data first (incremental — each fetcher
skips data already present / within its TTL, so only gaps are fetched),
then runs the three dimension builders -> aggregate -> the terminal preview
outputs: results.csv (eligible repos + value_score + risk_score), data.csv
(the measurements behind those scores), and preview.xlsx (the styled,
filterable workbook). Pass --skip-fetch to skip all fetchers and
only re-run the builders/aggregate/preview steps from existing data.

Usage:
    uv run python -m src.eligibility.run_eligibility_pipeline                # fetch + build
    uv run python -m src.eligibility.run_eligibility_pipeline --skip-fetch   # build only
    uv run python -m src.eligibility.run_eligibility_pipeline --from aggregate
    uv run python -m src.eligibility.run_eligibility_pipeline --list
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

FETCHERS = [
    # repo state — archived flag + GitHub license fallback
    Step("repo-owner",    "src.sources.github.fetch_repo_owner_data",     fetch=True, pgroup="meta"),
    # GitLab project metadata — the GitLab license fallback (project API
    # Licensee detection into gitlab/repos.csv, 90-day TTL)
    Step("gitlab-projects", "src.sources.gitlab.fetch_project_data",      fetch=True, pgroup="meta"),
    # license signals (homebrew before cpp — cpp joins homebrew's licenses)
    Step("spdx",          "src.sources.spdx.fetch_licenses",              fetch=True, pgroup="meta"),
    Step("osi",           "src.sources.osi.fetch_licenses",               fetch=True, pgroup="meta"),
    Step("npm-lic",       "src.sources.npm.fetch_licenses",               fetch=True, pgroup="lic"),
    Step("pypi-lic",      "src.sources.pypi.fetch_licenses",              fetch=True, pgroup="lic"),
    Step("crates-lic",    "src.sources.crates.fetch_licenses",            fetch=True, pgroup="lic"),
    Step("homebrew-lic",  "src.sources.homebrew.fetch_licenses",          fetch=True, pgroup="lic"),
    Step("cpp-lic",       "src.sources.cpp.fetch_licenses",               fetch=True),
    # registry EOL signals — advisory inputs to the manual `eol` override
    # in data/eligibility/overrides.csv (see build_active)
    Step("npm-eol",       "src.sources.npm.check_eol",                    fetch=True, pgroup="eol"),
    Step("pypi-eol",      "src.sources.pypi.check_eol",                   fetch=True, pgroup="eol"),
    Step("crates-eol",    "src.sources.crates.check_eol",                 fetch=True, pgroup="eol"),
    Step("cpp-eol",       "src.sources.cpp.check_eol",                    fetch=True, pgroup="eol"),
    # The per-contributor commit log — the bus-factor maintainer set behind
    # `bf_maintainer_fundable`. Serial and BEFORE the funding group: both
    # `maintainer-sponsors` (which picks WHICH logins to query from it) and
    # `funding-build` read its output, so a repo entering scope with no
    # contributor rows would silently score as un-fundable. 90-day TTL.
    Step("bf-contributors", "src.sources.github.fetch_contributors_metrics", fetch=True, net=True),
    # funding-intent signals (moved here from the risk pipeline)
    Step("funding-yml",   "src.sources.github.fetch_funding_yml",         fetch=True, pgroup="funding"),
    # outbound sponsorships — who each repo's owner funds (90-day TTL)
    Step("sponsorships",  "src.sources.github.fetch_sponsorships",        fetch=True, net=True, pgroup="funding"),
    Step("gitlab-funding", "src.sources.gitlab.fetch_funding_files",      fetch=True, pgroup="funding"),
    Step("npm-funding",   "src.sources.npm.fetch_funding",                fetch=True, pgroup="funding"),
    Step("pypi-funding",  "src.sources.pypi.fetch_funding",               fetch=True, pgroup="funding"),
    Step("sponsors",      "src.sources.github.fetch_sponsors",            fetch=True, pgroup="funding"),
    # personal Sponsors of each repo's bus-factor maintainers (reads the
    # `bf-contributors` step's github/contributor-commits.csv)
    Step("maintainer-sponsors", "src.sources.github.fetch_maintainer_sponsors", fetch=True, pgroup="funding"),
    Step("floss-fund",    "src.sources.floss_fund.funding_json",          fetch=True, pgroup="funding"),
    Step("oc-collectives", "src.sources.opencollective.fetch_collectives", fetch=True, pgroup="funding"),
    Step("opencollective", "src.sources.opencollective.fetch_budgets",    fetch=True),  # reads oc-collectives output — stays serial after the funding group
    # FOSS-foundation rosters → host-by-repo (nonprofit / host signal)
    Step("apache",        "src.sources.funding.apache",                   fetch=True, pgroup="rosters"),
    Step("cncf",          "src.sources.funding.cncf",                     fetch=True, pgroup="rosters"),
    Step("eclipse",       "src.sources.funding.eclipse",                  fetch=True, pgroup="rosters"),
    Step("fsf",           "src.sources.funding.fsf",                      fetch=True, pgroup="rosters"),
    Step("gnome",         "src.sources.funding.gnome",                    fetch=True, pgroup="rosters"),
    Step("gnu",           "src.sources.funding.gnu",                      fetch=True, pgroup="rosters"),
    Step("lf",            "src.sources.funding.lf",                       fetch=True, pgroup="rosters"),
    Step("xorg",          "src.sources.funding.xorg",                     fetch=True, pgroup="rosters"),
    Step("numfocus",      "src.sources.funding.numfocus",                 fetch=True, pgroup="rosters"),
    Step("openjs",        "src.sources.funding.openjs",                   fetch=True, pgroup="rosters"),
    Step("psf",           "src.sources.funding.psf",                      fetch=True, pgroup="rosters"),
    Step("sfc",           "src.sources.funding.sfc",                      fetch=True, pgroup="rosters"),
    Step("match-hosts",   "src.sources.funding.match_repos",              fetch=True),
]
BUILDERS = [
    Step("licenses",      "src.eligibility.build_licenses"),
    Step("active",        "src.eligibility.build_active"),
    Step("funding-build", "src.eligibility.build_funding"),
    Step("aggregate",     "src.eligibility.build_eligibility"),
    # The data/preview deliverables (results/people/xlsx) moved to their own
    # runner: src.run_preview_pipeline — they roll up ALL three stages, not
    # just eligibility.
]


def main() -> int:
    parser = build_parser("eligibility pipeline runner")
    args = parser.parse_args()
    steps = FETCHERS + BUILDERS
    return run_pipeline(steps, args)


if __name__ == "__main__":
    raise SystemExit(main())
