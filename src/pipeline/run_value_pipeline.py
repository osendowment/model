"""Value pipeline runner — ecosystem pipelines, then unify + verify.

Runs the four ecosystem pipelines (npm, crates, pypi, cpp — cpp itself
composes Debian + Homebrew), then the cross-ecosystem value steps:
ecosystem-download totals, git-URL classification, the unified per-repo
value table, and git-URL verification.

Usage:
    uv run python -m src.pipeline.run_value_pipeline
    uv run python -m src.pipeline.run_value_pipeline --skip-fetch
    uv run python -m src.pipeline.run_value_pipeline --from unify
    uv run python -m src.pipeline.run_value_pipeline --list
"""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("npm",       "src.pipeline.value.npm_pipeline",     fetch=True, pipeline=True),
    Step("crates",    "src.pipeline.value.crates_pipeline",  fetch=True, pipeline=True),
    Step("pypi",      "src.pipeline.value.pypi_pipeline",    fetch=True, pipeline=True),
    Step("cpp",       "src.pipeline.value.cpp_pipeline",     fetch=True, pipeline=True),
    Step("downloads", "src.pipeline.value.build_ecosystem_downloads"),
    Step("git-urls",  "src.pipeline.value.build_git_urls"),
    Step("unify",     "src.pipeline.value.unify_value_data"),
    Step("verify",    "src.pipeline.value.verify_git_urls"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("value pipeline runner").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
