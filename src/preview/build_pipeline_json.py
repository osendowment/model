#!/usr/bin/env python3
"""Build data/preview/pipeline.json — the pipeline numbers for the dashboard.

Same figures as the workbook's `pipeline` sheet, same generator: every count
comes from `scripts/stats.py` reading the live CSVs. The sheet renders
`markdown()`, this step writes `pipeline_json()` — one computation, two
deliverables, so the dashboard and the workbook can never disagree.

This runs as a pipeline step because `data/preview/` is rebuilt by
`src.preview.run_preview_pipeline` and nowhere else. Emitting the JSON only
from a hand-run `scripts/stats.py --json` left it pinned to whichever run last
happened to be typed by hand, while `preview.xlsx` moved on.

Usage:
    uv run python -m src.preview.build_pipeline_json
"""
import json

from rich.console import Console

from scripts.stats import (
    DEFAULT_JSON_PATH,
    eligibility_stats,
    pipeline_json,
    risk_stats,
    value_stats,
)

console = Console()


def main() -> int:
    payload = pipeline_json(value_stats(), risk_stats(), eligibility_stats())
    DEFAULT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_JSON_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    # One short line: the runner echoes a quick step's LAST output line, so a
    # message that wraps would surface as a meaningless tail fragment.
    n = payload["value"]["git_urls"]["valid"]["A"]
    console.print(f"Wrote data/preview/pipeline.json — {n} valid class-A repos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
