"""Value pipeline runner — ecosystem pipelines, then unify + validate.

Runs the four ecosystem pipelines (npm, crates, pypi, cpp — cpp itself
composes Debian + Homebrew), then the cross-ecosystem value steps:
per-ecosystem stats (data/value/stats.csv), git-URL classification, the
unified per-repo value table, and the validation rollup (`validation.csv`
+ the per-repo `valid` column).

Usage:
    uv run python -m src.value.run_value_pipeline
    uv run python -m src.value.run_value_pipeline --offline
    uv run python -m src.value.run_value_pipeline --refresh
    uv run python -m src.value.run_value_pipeline --rollup
    uv run python -m src.value.run_value_pipeline --from unify
    uv run python -m src.value.run_value_pipeline --list
"""
from src.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("npm",        "src.value.npm_pipeline",     fetch=True, pipeline=True, pgroup="eco"),
    Step("crates",     "src.value.crates_pipeline",  fetch=True, pipeline=True, pgroup="eco"),
    Step("pypi",       "src.value.pypi_pipeline",    fetch=True, pipeline=True, pgroup="eco"),
    Step("cpp",        "src.value.cpp_pipeline",     fetch=True, pipeline=True, pgroup="eco"),
    Step("stats",      "src.value.build_stats"),
    Step("git-urls",   "src.value.build_git_urls"),
    Step("eco-fetch",  "src.sources.ecosystems.candidates",      net=True),
    Step("resolve",    "src.value.apply_ecosystems_authority",   net=True),
    Step("unify",      "src.value.unify_value_data"),
    Step("validation", "src.value.build_validation",             net=True),
    Step("criticality", "src.value.apply_criticality"),
]

# Audit-only steps — read-only diffs whose output feeds NO value.csv column
# and is read by nothing downstream, so the pipeline does not run them (same
# discipline as the risk runner's AUDIT_FETCHERS). Still runnable by hand:
#   uv run python -m src.value.audit_ecosystems   # diff packages.csv vs value.csv
# (writes data/sources/ecosystems/audit.csv, consumed by no other script.)
AUDIT_STEPS = [
    Step("eco-audit",  "src.value.audit_ecosystems"),
]

# The cross-ecosystem rollup that turns existing per-ecosystem results.csv into
# value.csv. Reads only the per-eco results.csv + the validation caches +
# overrides.csv — no raw ecosystem data (e.g. the crates db-dump in the
# gitignored tmp/), so it runs even on a checkout where those large
# regenerable inputs have never been downloaded.
#
# Net steps use TTL caches — pass --offline to hard-forbid network (pure-cache
# run) or --refresh to force refetch ignoring TTL.
#
#   eco-fetch   — pull ecosyste.ms repo identity for every top package (network)
#   resolve     — rewrite results.csv git/github_repo: override > eco > prior
#   unify       — merge per-eco results into value.csv
#   validation  — ls-remote non-GitHub URLs; stamp git_valid on value.csv
#   criticality — stamp the OpenSSF criticality score onto value.csv
ROLLUP_LABELS = ("eco-fetch", "resolve", "unify", "validation", "criticality")


def main() -> int:
    parser = build_parser("value pipeline runner")
    parser.add_argument(
        "--rollup", action="store_true",
        help="Run only the cross-ecosystem rollup (eco-fetch → resolve → unify → "
             "validation → criticality) that turns existing per-eco results.csv into "
             "value.csv. Skips the ecosystem sub-pipelines, stats, and URL "
             "re-derivation, so no raw ecosystem data is needed.",
    )
    args = parser.parse_args()
    steps = [s for s in STEPS if s.label in ROLLUP_LABELS] if args.rollup else STEPS
    return run_pipeline(steps, args)


if __name__ == "__main__":
    raise SystemExit(main())
