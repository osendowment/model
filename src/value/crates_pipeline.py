"""crates.io ecosystem pipeline — fetch raw crates data, then build outputs."""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("fetch-db-dump",   "src.sources.crates.fetch_db_dump",           fetch=True, net=True),
    Step("fetch-downloads", "src.sources.crates.fetch_version_downloads", fetch=True, net=True),
    Step("process",         "src.sources.crates.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("crates ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
