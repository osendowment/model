"""C/C++ ecosystem pipeline — Debian + Homebrew sub-pipelines, then cpp aggregation.

The cpp ecosystem is built from Debian + Homebrew C/C++ packages (joined via
Repology), so this orchestrator runs both sub-pipelines first, then the cpp
aggregation in `src/sources/cpp/process_data.py`.
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("debian",         "src.value.debian_pipeline",   fetch=True, pipeline=True),
    Step("homebrew",       "src.value.homebrew_pipeline", fetch=True, pipeline=True),
    Step("fetch-repology", "src.sources.cpp.fetch_repology_urls",          fetch=True),
    Step("process",        "src.sources.cpp.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("cpp ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
