#!/bin/bash
# Run the full model pipeline: Value → Risk → Eligibility → health check.
#
# Usage:
#   scripts/run-pipeline.sh              # normal run (TTL-cached, ~1-1.5 min warm)
#   scripts/run-pipeline.sh --refresh    # force refetch past every TTL
#   scripts/run-pipeline.sh --offline    # pure-cache run, no network
#
# Any arguments are passed through to all three stage runners.
set -o pipefail
cd "$(dirname "$0")/.." || exit 1

t0=$(date +%s)
for stage in value.run_value_pipeline risk.run_risk_pipeline eligibility.run_eligibility_pipeline; do
  echo
  echo "=== src.$stage $* ==="
  uv run python -m "src.$stage" "$@" || { echo "ABORTED: src.$stage failed"; exit 1; }
done

echo
uv run python scripts/pipeline_health.py | tail -3
echo "Total: $(( $(date +%s) - t0 ))s"
