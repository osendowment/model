"""PyPI ecosystem pipeline — fetch raw PyPI data, then build value outputs."""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("fetch-data", "src.sources.pypi.fetch_pypi_data", fetch=True),
    Step("fetch-urls", "src.sources.pypi.fetch_pypi_urls", fetch=True),
    Step("process",    "src.sources.pypi.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("pypi ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
