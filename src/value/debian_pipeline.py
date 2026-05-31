"""Debian ecosystem pipeline — fetch raw Debian data, then build outputs.

Feeds the cpp ecosystem (`cpp_pipeline.py` composes Debian + Homebrew).
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("fetch-data", "src.sources.debian.fetch_debian_data", fetch=True),
    Step("process",    "src.sources.debian.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("debian ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
