"""npm ecosystem pipeline — fetch raw npm data, then build value outputs."""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("fetch-data",  "src.sources.npm.fetch_npm_data",      fetch=True),
    Step("fetch-stats", "src.sources.npm.fetch_npm_stats",     fetch=True),
    Step("fetch-repos", "src.sources.npm.fetch_nice_registry", fetch=True, net=True),
    Step("process",     "src.sources.npm.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("npm ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
