"""Shared model parameters loaded from src/pipeline/settings.json."""

import csv
import json
import os

# params.py lives in src/pipeline/common/; settings.json is one level up.
_PARAMS_PATH = os.path.join(os.path.dirname(__file__), "..", "settings.json")

with open(_PARAMS_PATH, encoding="utf-8") as _f:
    _P = json.load(_f)

# Top package selection
TOP_THRESHOLD_PCT: float = _P["top_selection"]["threshold_pct"]

# PageRank
PAGERANK_ALPHA: float = _P["pagerank"]["alpha"]

# Downloads score — linear combination of per-ecosystem avg installs used as
# the unified importance signal for multi-ecosystem pipelines (e.g. cpp/).
DOWNLOADS_SCORE_DEBIAN_WEIGHT:   float = _P["downloads_score"]["debian_weight"]
DOWNLOADS_SCORE_HOMEBREW_WEIGHT: float = _P["downloads_score"]["homebrew_weight"]

# Value class cutoffs (cumulative PageRank share)
VALUE_CLASS_A: float = _P["value_classes"]["A"]
VALUE_CLASS_B: float = _P["value_classes"]["B"]
VALUE_CLASS_C: float = _P["value_classes"]["C"]

# Years
YEARS: list[int] = _P["years"]

# Risk dimensions are scored by direction-aware risk percentiles
# (src.pipeline.common.percentiles), not A/B/C/D classes — no thresholds here.

# Risk-pipeline input scope — which value classes feed the risk pipeline.
RISK_INPUT_CLASSES: list[str] = _P["risk_input"]["value_classes"]


_STATS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "value", "stats.csv")


def ecosystem_avg_downloads(ecosystem: str) -> int:
    """Return the average annual total downloads for an ecosystem across YEARS.

    Reads the `downloads_<year>` rows of `data/value/stats.csv` (a metric-row
    × ecosystem-column matrix). Years with 0 (or blank) recorded downloads are
    treated as missing data, not zero, and excluded from both numerator and
    denominator — otherwise a gap in the source (e.g. no Wayback snapshot for
    Homebrew 2021) would deflate the average by ~20% for each missing year.
    """
    with open(_STATS_PATH, encoding="utf-8") as f:
        by_metric = {row["metric"]: row for row in csv.DictReader(f)}
    populated = []
    for y in YEARS:
        cell = (by_metric.get(f"downloads_{y}", {}).get(ecosystem) or "").strip()
        if cell and int(cell) > 0:
            populated.append(int(cell))
    return sum(populated) // len(populated) if populated else 0


def assign_value_class(cumulative_share: float) -> str:
    """Assign A/B/C/D based on cumulative PageRank share."""
    if cumulative_share <= VALUE_CLASS_A:
        return "A"
    if cumulative_share <= VALUE_CLASS_B:
        return "B"
    if cumulative_share <= VALUE_CLASS_C:
        return "C"
    return "D"
