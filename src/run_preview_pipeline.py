"""Preview pipeline runner — the cross-stage deliverables in data/preview/.

Terminal rollup of all three stages (Value → Risk → Eligibility): the
eligible-repo table with value/risk scores, the measurements behind those
scores, and the styled workbook. Runs after the eligibility pipeline;
every file in data/preview/ is rebuilt here and nowhere else.

Usage:
    uv run python -m src.run_preview_pipeline
    uv run python -m src.run_preview_pipeline --only preview-xlsx
    uv run python -m src.run_preview_pipeline --list
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    # Terminal cross-stage rollup: eligible repos + value_score + risk_score.
    Step("results",      "src.build_results"),
    # The measurements those scores were computed from, same repos, same order.
    Step("data",         "src.build_data"),
    # repos.csv + methodology + pipeline stats as one styled, filterable workbook.
    Step("preview-xlsx", "src.build_preview_workbook"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("preview pipeline runner").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
