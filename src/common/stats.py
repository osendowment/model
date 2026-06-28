"""Shared statistics helpers for the risk-percentile system.

Every risk dimension turns its raw metrics into direction-aware risk
percentiles (`_p`, 0-100, higher = riskier), then combines a chosen subset
into a per-dimension composite `<dim>_p` via a geometric mean. These pure
functions are the shared primitives.
"""

import bisect


def risk_percentiles(values: list[float], higher_is_worse: bool) -> list[float | None]:
    """Worst-pinned CDF risk percentile (0-100), direction-aware.

    `values` are the present, parseable metric values being ranked. For each
    value v_i over n values:

        higher_is_worse:  P_i = 100 * #{j : v_j <= v_i} / n
        lower_is_worse:   P_i = 100 * #{j : v_j >= v_i} / n

    The single worst value maps to exactly 100; the best to >= 100/n > 0, so a
    geometric mean over these can never collapse to 0. If every value is
    identical the axis carries no signal -> returns [None] * n. Empty -> [].
    """
    n = len(values)
    if n == 0:
        return []
    if min(values) == max(values):
        return [None] * n
    ordered = sorted(values)
    out: list[float | None] = [0.0] * n
    for i, v in enumerate(values):
        if higher_is_worse:
            count = bisect.bisect_right(ordered, v)
        else:
            count = n - bisect.bisect_left(ordered, v)
        out[i] = 100.0 * count / n
    return out


def risk_percentiles_aligned(
    values: list[float | None], higher_is_worse: bool
) -> list[float | None]:
    """`risk_percentiles` over a list that may contain None (missing) values.

    Present values are ranked among themselves; missing values stay None and
    are excluded from n. A constant present-axis yields None for every repo.
    The result is aligned 1:1 with the input.
    """
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    out: list[float | None] = [None] * len(values)
    if not present:
        return out
    ranked = risk_percentiles([v for _, v in present], higher_is_worse)
    for (i, _), p in zip(present, ranked):
        out[i] = p
    return out


def floor_anchored_risk(
    values: list[float | None], floor: float = 0.0, anchor: float = 50.0
) -> list[float | None]:
    """Risk score (0-100) that pins the floor value to a neutral `anchor`.

    Unlike `risk_percentiles`, where the most-common floor value lands wherever
    its CDF puts it (e.g. 78 when 78% of repos share `floor`), this anchors
    every value == `floor` to `anchor` — a deliberate "neutral, not safe"
    baseline (0 known CVEs is not proof of security, just absence of evidence).
    Values strictly above the floor are ranked among *themselves* by worst-
    pinned CDF (more = worse) and mapped linearly into (`anchor`, 100], so the
    single worst value maps to 100. Missing (None) stays None; aligned 1:1 with
    the input.
    """
    out: list[float | None] = [None] * len(values)
    above = [(i, v) for i, v in enumerate(values) if v is not None and v > floor]
    for i, v in enumerate(values):
        if v is not None and v <= floor:
            out[i] = float(anchor)
    if above:
        ordered = sorted(v for _, v in above)
        m = len(above)
        for i, v in above:
            cdf = bisect.bisect_right(ordered, v) / m   # (0, 1], worst -> 1
            out[i] = anchor + (100.0 - anchor) * cdf
    return out


def geometric_mean(values: list[float]) -> float:
    """Geometric mean (prod v)^(1/n). Assumes every value > 0; [] -> 0.0."""
    if not values:
        return 0.0
    product = 1.0
    for v in values:
        product *= v
    return product ** (1.0 / len(values))


def geom_mean_composite(rows: list[list[float | None]]) -> list[float | None]:
    """Per-repo geometric mean of component percentiles.

    `rows` is one list per repo of its component `_p`s (floats or None).
    Returns the geometric mean when the row is non-empty and every component
    is present, else None — the per-dimension composite `<dim>_p`.
    """
    out: list[float | None] = []
    for comps in rows:
        if not comps or any(c is None for c in comps):
            out.append(None)
        else:
            out.append(geometric_mean(comps))
    return out
