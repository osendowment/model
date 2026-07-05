"""Shared per-builder helper to attach risk-percentile columns to CSV rows."""

from collections.abc import Callable

from src.common.stats import geom_mean_composite, risk_percentiles_aligned


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
    neutral_fill: dict[str, float] | None = None,
    composite_fn: Callable[
        [list[list[float | None]]], list[float | None]
    ] = geom_mean_composite,
) -> None:
    """In place: add `<metric>_p` for each (metric, higher_is_worse) spec, then
    `dim_col` = `composite_fn` of the already-computed `composite_cols` _p's
    (default: geometric mean; pass `max_composite` for worst-of aggregation).

    A repo with a missing/unparseable metric gets `<metric>_p = ""`; a constant
    axis yields "" for every repo. The composite is "" unless every component
    `_p` is present.

    `neutral_fill` maps a `<metric>_p` column to a neutral percentile (e.g. 50).
    After percentiles are computed, an empty cell in such a column is set to that
    value *only when every other `composite_cols` component is present* for that
    row — so the fill rescues a row that would otherwise score, but never invents
    a lone percentile on a row that stays blank anyway. Columns absent from
    `neutral_fill` keep the strict "missing -> blanks the score" behaviour.
    """
    for col, higher_is_worse in pctl_specs:
        ps = risk_percentiles_aligned(
            [_cell(r.get(col, "")) for r in rows], higher_is_worse=higher_is_worse
        )
        for r, p in zip(rows, ps):
            r[col + "_p"] = "" if p is None else round(p, 2)
    for pcol, fill in (neutral_fill or {}).items():
        others = [c for c in composite_cols if c != pcol]
        for r in rows:
            if r.get(pcol, "") in ("", None) and all(
                r.get(c, "") not in ("", None) for c in others
            ):
                r[pcol] = fill
    comp_rows = [[_cell(r.get(c, "")) for c in composite_cols] for r in rows]
    for r, c in zip(rows, composite_fn(comp_rows)):
        # Composite score: integer 0-100, higher = riskier. Floored at 1 — the
        # worst-pinned percentile is always > 0, so a rounded-down 0 is just the
        # lowest-risk tier, not "no risk"; this also keeps the overall risk
        # geometric mean (over component scores) from collapsing to 0.
        r[dim_col] = "" if c is None else max(1, int(round(c)))
