"""Value pipeline runner — ecosystem pipelines, then unify + verify.

Runs the four ecosystem pipelines (npm, crates, pypi, cpp — cpp itself
composes Debian + Homebrew), then the cross-ecosystem value steps:
per-ecosystem stats (data/value/stats.csv), git-URL classification, the
unified per-repo value table, git-URL verification, and the validation
rollup (`validation.csv` + the per-repo `valid` column).

Usage:
    uv run python -m src.value.run_value_pipeline
    uv run python -m src.value.run_value_pipeline --skip-fetch
    uv run python -m src.value.run_value_pipeline --from unify
    uv run python -m src.value.run_value_pipeline --list
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("npm",       "src.value.npm_pipeline",     fetch=True, pipeline=True),
    Step("crates",    "src.value.crates_pipeline",  fetch=True, pipeline=True),
    Step("pypi",      "src.value.pypi_pipeline",    fetch=True, pipeline=True),
    Step("cpp",        "src.value.cpp_pipeline",     fetch=True, pipeline=True),
    Step("stats",      "src.value.build_stats"),
    Step("git-urls",   "src.value.build_git_urls"),
    Step("unify",      "src.value.unify_value_data"),
    Step("verify",     "src.value.verify_git_urls"),
    Step("validation", "src.value.build_validation"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("value pipeline runner").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
