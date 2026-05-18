"""npm ecosystem pipeline — fetch raw npm data, then build value outputs."""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("fetch-data",  "src.npm.fetch_npm_data",      fetch=True),
    Step("fetch-stats", "src.npm.fetch_npm_stats",     fetch=True),
    Step("fetch-repos", "src.npm.fetch_nice_registry", fetch=True),
    Step("process",     "src.npm.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("npm ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
