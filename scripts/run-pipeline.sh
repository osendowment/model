#!/bin/bash
# Run the model pipeline. Stages, in order:
#
#     value → risk → eligibility → preview → health
#
# `health` (scripts/pipeline_health.py) is the last stage and runs by default:
# a red check is a bug, so it aborts with a non-zero exit like any other stage.
#
# Usage:
#   scripts/run-pipeline.sh                       # every stage (TTL-cached, ~2 min warm)
#   scripts/run-pipeline.sh --refresh             # force refetch past every TTL
#   scripts/run-pipeline.sh --offline             # pure-cache run, no network
#
#   scripts/run-pipeline.sh --stage risk          # ONE stage
#   scripts/run-pipeline.sh --from-stage risk     # that stage through to the end (incl. health)
#   scripts/run-pipeline.sh --stage risk --from scorecard   # one stage, from a step
#   scripts/run-pipeline.sh --stage value --only unify      # one stage, one step
#   scripts/run-pipeline.sh --no-health           # drop the health stage
#
#   scripts/run-pipeline.sh --list-stages
#   scripts/run-pipeline.sh --stage risk --list   # the steps of that stage
#
# --stage / --from-stage / --no-health are consumed here. EVERY other argument
# is passed through verbatim to the stage runners — so --refresh / --offline,
# and the runners' own per-step --from STEP / --only STEP / --list flags, keep
# working. Stage selection needs its own flag names precisely because --from
# already means "from this STEP" to a stage runner. (The health stage takes no
# arguments, so passthrough is never forwarded to it.)
#
# `--stage <x>` runs that stage ALONE, so it skips health — a single stage
# leaves the later ones stale and health would rightly complain. Use
# `--from-stage <x>` to run through to the end and be told the truth.
set -o pipefail
cd "$(dirname "$0")/.." || exit 1

STAGES=(value risk eligibility preview health)

module_for() {
  case "$1" in
    value)       echo "src.value.run_value_pipeline" ;;
    risk)        echo "src.risk.run_risk_pipeline" ;;
    eligibility) echo "src.eligibility.run_eligibility_pipeline" ;;
    preview)     echo "src.run_preview_pipeline" ;;
    health)      echo "scripts.pipeline_health" ;;
    *)           return 1 ;;
  esac
}

is_stage() {
  local s="$1" x
  for x in "${STAGES[@]}"; do [ "$x" = "$s" ] && return 0; done
  return 1
}

die() { echo "run-pipeline: $*" >&2; exit 2; }

require_stage() {
  [ -n "$1" ] || die "$2 needs a stage name (one of: ${STAGES[*]})"
  is_stage "$1" || die "unknown stage '$1' (one of: ${STAGES[*]})"
}

only_stage=""
from_stage=""
no_health=0
passthrough=()

while [ $# -gt 0 ]; do
  case "$1" in
    --stage)        only_stage="$2"; shift 2 ;;
    --stage=*)      only_stage="${1#*=}"; shift ;;
    --from-stage)   from_stage="$2"; shift 2 ;;
    --from-stage=*) from_stage="${1#*=}"; shift ;;
    --no-health)    no_health=1; shift ;;
    --list-stages)  printf '%s\n' "${STAGES[@]}"; exit 0 ;;
    -h|--help)      sed -n '2,33p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *)              passthrough+=("$1"); shift ;;
  esac
done

[ -n "$only_stage" ] && [ -n "$from_stage" ] && \
  die "--stage and --from-stage are mutually exclusive"

# Which stages to run.
run_list=()
if [ -n "$only_stage" ]; then
  require_stage "$only_stage" "--stage"
  run_list=("$only_stage")
elif [ -n "$from_stage" ]; then
  require_stage "$from_stage" "--from-stage"
  started=0
  for s in "${STAGES[@]}"; do
    [ "$s" = "$from_stage" ] && started=1
    [ "$started" -eq 1 ] && run_list+=("$s")
  done
else
  run_list=("${STAGES[@]}")
fi

if [ "$no_health" -eq 1 ]; then
  filtered=()
  for s in "${run_list[@]}"; do
    [ "$s" = "health" ] || filtered+=("$s")
  done
  run_list=("${filtered[@]}")
fi

[ ${#run_list[@]} -eq 0 ] && die "no stages to run"

t0=$(date +%s)
for stage in "${run_list[@]}"; do
  module=$(module_for "$stage")
  echo
  if [ "$stage" = "health" ]; then
    # Takes no arguments; its summary is the last few lines.
    echo "=== health ($module) ==="
    uv run python -m "$module" | tail -3 \
      || { echo "ABORTED: health check failed"; exit 1; }
  else
    echo "=== $stage ($module) ${passthrough[*]} ==="
    uv run python -m "$module" "${passthrough[@]}" \
      || { echo "ABORTED: $stage failed"; exit 1; }
  fi
done

echo "Total: $(( $(date +%s) - t0 ))s  [stages: ${run_list[*]}]"
