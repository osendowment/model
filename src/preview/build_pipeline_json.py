#!/usr/bin/env python3
"""Build data/preview/pipeline.json — the pipeline numbers for the dashboard.

The dashboard (app.endowment.dev/model) reads this instead of the xlsx: the
top-of-funnel counts (`tracked`, ~3.8M packages) come from multi-100MB git-LFS
source files a Worker cannot open, so they must be pre-computed here.

This step is where the pipeline's counts are computed — once. Reading them off
the live CSVs (via `scripts/stats.py`) takes ~2s, so the three stat dicts are
stored here verbatim and the workbook's `pipeline` sheet renders `markdown()`
over this file instead of recomputing them. One computation, two deliverables:
the dashboard and the sheet cannot disagree, because they are the same dict.

That makes this step a dependency of `preview-xlsx`, and it runs before it.

This is a step of the preview pipeline because `data/preview/` is rebuilt by
`src.preview.run_preview_pipeline` and nowhere else. Emitting the JSON from a
hand-run `scripts/stats.py --json` instead left it pinned to whichever run
last happened to be typed by hand, while `preview.xlsx` moved on.

Usage:
    uv run python -m src.preview.build_pipeline_json
"""
import json
from pathlib import Path

from rich.console import Console

from scripts.stats import (
    eligibility_stats,
    funnel_stats,
    risk_stats,
    value_stats,
)

console = Console()

ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_FILE = ROOT / "data" / "preview" / "pipeline.json"


def pipeline_json(v: dict, r: dict, e: dict) -> dict:
    """The same numbers as the pipeline sheet, as a machine-readable payload."""
    funnel = [
        {"stage": stage, "count": count, "denom": denom,
         "denom_label": denom_label, "comment": comment}
        for stage, count, denom, denom_label, comment in funnel_stats(v, r, e)
    ]
    return {"version": 1, "value": v, "risk": r,
            "eligibility": e, "funnel": funnel}


def main() -> int:
    payload = pipeline_json(value_stats(), risk_stats(), eligibility_stats())
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    # One short line: the runner echoes a quick step's LAST output line, so a
    # message that wraps would surface as a meaningless tail fragment.
    n = payload["value"]["git_urls"]["valid"]["A"]
    console.print(f"Wrote data/preview/pipeline.json — {n} valid class-A repos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
