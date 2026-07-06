"""Homebrew ecosystem pipeline — fetch raw Homebrew data, then build outputs.

Feeds the cpp ecosystem (`cpp_pipeline.py` composes Debian + Homebrew).
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("fetch-data", "src.sources.homebrew.fetch_homebrew_data", fetch=True, net=True),
    Step("process",    "src.sources.homebrew.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("homebrew ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
