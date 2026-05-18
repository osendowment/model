"""Homebrew ecosystem pipeline — fetch raw Homebrew data, then build outputs.

Feeds the cpp ecosystem (`cpp_pipeline.py` composes Debian + Homebrew).
"""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("fetch-data", "src.homebrew.fetch_homebrew_data", fetch=True),
    Step("process",    "src.homebrew.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("homebrew ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
