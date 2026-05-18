"""C/C++ ecosystem pipeline — Debian + Homebrew sub-pipelines, then cpp aggregation.

The cpp ecosystem is built from Debian + Homebrew C/C++ packages (joined via
Repology), so this orchestrator runs both sub-pipelines first, then the cpp
aggregation in `src/cpp/process_data.py`.
"""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("debian",         "src.pipeline.value.debian_pipeline",   fetch=True, pipeline=True),
    Step("homebrew",       "src.pipeline.value.homebrew_pipeline", fetch=True, pipeline=True),
    Step("fetch-repology", "src.cpp.fetch_repology_urls",          fetch=True),
    Step("process",        "src.cpp.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("cpp ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
