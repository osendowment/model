"""Shared model parameters loaded from src/settings.json."""

import csv
import json
import os

# params.py lives in src/common/; settings.json is one level up.
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

# Value class intervals (cumulative PageRank share), [lo, hi] per class in
# settings.json. The upper bound is the classification cutoff — intervals are
# treated as (lo, hi] (upper bound inclusive); see assign_value_class.
VALUE_CLASS_A: float = _P["value_classes"]["A"][1]
VALUE_CLASS_B: float = _P["value_classes"]["B"][1]

# Years — the risk-stage window. The whole risk pipeline (concentration,
# complexity, security, workload) shares this window; LAST_COMPLETE_YEAR is the
# EOY-snapshot anchor and the '_full' cap. Year boundaries live here, never in
# output column names.
YEARS: list[int] = _P["years"]
LAST_COMPLETE_YEAR: int = max(YEARS)

# Risk dimensions are scored by direction-aware risk percentiles
# (src.pipeline.common.percentiles), not A/B/C/D classes — no thresholds here.

# Risk-pipeline input scope — which value classes feed the risk pipeline.
RISK_INPUT_CLASSES: list[str] = _P["risk_input"]["value_classes"]

# load_top_repos scope — the platforms and value classes that define the "top
# repos" set shared by the risk and eligibility stages. A value.csv row is a top
# repo iff its `platform` is in TOP_REPO_PLATFORMS, its `class` is in
# TOP_REPO_CLASSES, and it is git_valid. GitHub-only today; the gate is
# host-agnostic so adding "gitlab" here (once gl/ enrichment lands) pulls in
# valid GitLab repos without further code changes.
TOP_REPO_PLATFORMS: set[str] = set(_P["top_repos"]["platforms"])
TOP_REPO_CLASSES: set[str] = set(_P["top_repos"]["classes"])

# Value score — value.csv `score`, a 0–100 pro-rata blend of up to three
# components: OpenSSF criticality (openssf_crit, stored ·100 as 0-100), the ecosyste.ms
# critical flag (eco_crit, stored 0/100), and ecosystem centrality (top_eco_pct,
# a PageRank percentile already 0-100). Only present (non-blank) components are
# weighted, then renormalized by their weight sum; a row needs at least
# MIN_COMPONENTS present or `score` is blank. Criticality-dominant (0.6/0.2/0.2)
# so foundational-but-quiet micro-deps don't outrank genuinely critical
# projects; see docs/value.md.
VALUE_SCORE_CRIT_WEIGHT:       float = _P["value_score"]["criticality_weight"]
VALUE_SCORE_ECO_CRIT_WEIGHT:   float = _P["value_score"]["eco_crit_weight"]
VALUE_SCORE_CENTRALITY_WEIGHT: float = _P["value_score"]["centrality_weight"]
VALUE_SCORE_PR_WEIGHT:         float = _P["value_score"]["pr_score_weight"]
VALUE_SCORE_MIN_COMPONENTS:    int   = _P["value_score"]["min_components"]

# Concentration window length (complete years). The bus-factor/HHI '_5y'
# columns cover the last CONCENTRATION_WINDOW_YEARS complete years, anchored
# to LAST_COMPLETE_YEAR; '_full' caps at LAST_COMPLETE_YEAR.
CONCENTRATION_WINDOW_YEARS: int = _P["concentration"]["window_years"]

# Bus-factor ownership threshold — the commit share that defines the bus
# factor (fewest contributors whose combined commits reach this fraction).
BUS_FACTOR_THRESHOLD: float = _P["concentration"]["bus_factor_threshold"]

# Key-contributor threshold for data/preview/people.csv — independently
# tunable from BUS_FACTOR_THRESHOLD (used by risk/eligibility scoring).
KEY_CONTRIBUTORS_CUM_SHARE: float = _P["key_contributors"]["cum_share"]


_STATS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "value", "stats.csv")


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
    """Assign A/B/C based on cumulative PageRank share."""
    if cumulative_share <= VALUE_CLASS_A:
        return "A"
    if cumulative_share <= VALUE_CLASS_B:
        return "B"
    return "C"
