"""Shared per-builder helper to attach risk-percentile columns to CSV rows."""

from src.pipeline.common.stats import geom_mean_composite, risk_percentiles_aligned


def _cell(value: str | float | int | None) -> float | None:
    """Parse a CSV cell to float; blank or unparseable -> None (missing)."""
    if isinstance(value, (int, float)):
        return float(value)
    s = (value or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def add_percentiles(
    rows: list[dict],
    pctl_specs: list[tuple[str, bool]],
    composite_cols: list[str],
    dim_col: str,
) -> None:
    """In place: add `<metric>_p` for each (metric, higher_is_worse) spec, then
    `dim_col` = geometric mean of the already-computed `composite_cols` _p's.

    A repo with a missing/unparseable metric gets `<metric>_p = ""`; a constant
    axis yields "" for every repo. The composite is "" unless every component
    `_p` is present.
    """
    for col, higher_is_worse in pctl_specs:
        ps = risk_percentiles_aligned(
            [_cell(r.get(col, "")) for r in rows], higher_is_worse=higher_is_worse
        )
        for r, p in zip(rows, ps):
            r[col + "_p"] = "" if p is None else round(p, 2)
    comp_rows = [[_cell(r.get(c, "")) for c in composite_cols] for r in rows]
    for r, c in zip(rows, geom_mean_composite(comp_rows)):
        # Composite score: integer 0-100, higher = riskier. Floored at 1 — the
        # worst-pinned percentile is always > 0, so a rounded-down 0 is just the
        # lowest-risk tier, not "no risk"; this also keeps the overall risk
        # geometric mean (over component scores) from collapsing to 0.
        r[dim_col] = "" if c is None else max(1, int(round(c)))
