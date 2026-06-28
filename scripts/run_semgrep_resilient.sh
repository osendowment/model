#!/usr/bin/env bash
# Resilient semgrep wrapper: restart on crash, stop when no more missing.
set -u
cd "$(dirname "$0")/.."
LOG=/tmp/semgrep-loop.log
echo "[$(date +%H:%M:%S)] starting resilient semgrep loop" > "$LOG"
attempt=0
prev_count=0
while true; do
    attempt=$((attempt + 1))
    # Count risk-scope repos (semgrep's target set) via Python (quoted CSV breaks awk)
    want=$(uv run python -c "
import csv
n = sum(1 for r in csv.DictReader(open('data/risk/risk.csv')) if (r.get('repo') or '').strip())
print(n)" 2>/dev/null)
    have=$(tail -n +2 data/sources/git/semgrep.csv 2>/dev/null | cut -d, -f1 | sort -u | wc -l | tr -d ' ')
    miss=$(( want - have ))
    echo "[$(date +%H:%M:%S)] attempt=$attempt have=$have miss=$miss" >> "$LOG"
    if [ "$miss" -le 5 ]; then
        echo "[$(date +%H:%M:%S)] miss <= 5, stopping" >> "$LOG"
        break
    fi
    if [ "$attempt" -gt 1 ] && [ "$have" -eq "$prev_count" ]; then
        echo "[$(date +%H:%M:%S)] no progress in last attempt, stopping" >> "$LOG"
        break
    fi
    prev_count=$have
    uv run python -m src.sources.github.fetch_semgrep --rulepack p/default --concurrency 3 --limit 0 --max-disk-gb 2 >> /tmp/semgrep-loop-detail.log 2>&1
    rc=$?
    echo "[$(date +%H:%M:%S)] semgrep exited rc=$rc" >> "$LOG"
    if [ "$attempt" -gt 50 ]; then
        echo "[$(date +%H:%M:%S)] too many attempts, stopping" >> "$LOG"
        break
    fi
done
echo "[$(date +%H:%M:%S)] resilient loop exiting" >> "$LOG"
