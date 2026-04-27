"""Shared model parameters loaded from data/params.json."""

import csv
import json
import os

_PARAMS_PATH = os.path.join(os.path.dirname(__file__), "params.json")

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

# Risk classification
CONCENTRATION_THRESHOLDS: dict = _P["risk_classification"]["concentration"]
COMPLEXITY_LOC_THRESHOLDS: dict = _P["risk_classification"]["complexity_loc"]
ISSUE_DEBT_THRESHOLDS: dict = _P["risk_classification"]["issue_debt"]
ISSUE_TREND_THRESHOLDS: dict = _P["risk_classification"]["issue_trend"]


_ECOSYSTEM_DL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "ecosystem-downloads.csv")


def ecosystem_avg_downloads(ecosystem: str) -> int:
    """Return the average annual total downloads for an ecosystem across YEARS.

    Years with 0 recorded downloads are treated as missing data (not zero)
    and excluded from both numerator and denominator. Otherwise a gap in the
    source (e.g. no Wayback snapshot for Homebrew 2021) would deflate the
    average by ~20% for each missing year.
    """
    with open(_ECOSYSTEM_DL_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    populated = [
        int(row[ecosystem]) for row in rows
        if int(row["year"]) in YEARS and int(row[ecosystem]) > 0
    ]
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
