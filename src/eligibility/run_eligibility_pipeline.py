"""Eligibility pipeline runner — license / EOL / foundation checks, then classify.

By default runs only the classification step on existing data. Pass
--with-fetchers to prepend the per-ecosystem license + EOL fetchers, the
OSI license-list refresh, and the GitHub repo-owner / foundation fetchers.

Usage:
    uv run python -m src.eligibility.run_eligibility_pipeline                 # classify
    uv run python -m src.eligibility.run_eligibility_pipeline --with-fetchers # full
    uv run python -m src.eligibility.run_eligibility_pipeline --list
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

FETCHERS = [
    Step("npm-eol",      "src.sources.npm.check_eol",                fetch=True),
    Step("crates-eol",   "src.sources.crates.check_eol",             fetch=True),
    Step("pypi-eol",     "src.sources.pypi.check_eol",               fetch=True),
    Step("cpp-eol",      "src.sources.cpp.check_eol",                fetch=True),
    Step("npm-lic",      "src.sources.npm.fetch_licenses",           fetch=True),
    Step("crates-lic",   "src.sources.crates.fetch_licenses",        fetch=True),
    Step("pypi-lic",     "src.sources.pypi.fetch_licenses",          fetch=True),
    Step("cpp-lic",      "src.sources.cpp.fetch_licenses",           fetch=True),
    Step("homebrew-lic", "src.sources.homebrew.fetch_licenses",      fetch=True),
    Step("osi",          "src.sources.osi.fetch_licenses",           fetch=True),
    Step("repo-owner",   "src.sources.github.fetch_repo_owner_data", fetch=True),
    Step("foundations",  "src.sources.foundations.match_repos",      fetch=True),
]
STEPS = [Step("classify", "src.eligibility.classify_eligibility")]


def main() -> int:
    parser = build_parser("eligibility pipeline runner")
    parser.add_argument("--with-fetchers", action="store_true",
                        help="Also run the license / EOL / foundation fetchers first")
    args = parser.parse_args()
    steps = (FETCHERS + STEPS) if args.with_fetchers else STEPS
    return run_pipeline(steps, args)


if __name__ == "__main__":
    raise SystemExit(main())
