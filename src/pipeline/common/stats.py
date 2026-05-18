"""Shared statistics helpers for the geom_mean_quartile classifiers.

`build_workload.py` and `build_security.py` both classify repos A-D by
percentile-ranking metrics, taking a geometric mean of the percentiles,
and bucketing the result into equal-count quartiles. These three pure
functions are the shared primitives of that method.
"""


def hazen_percentiles(values: list[float]) -> list[float]:
    """Percentile-rank each value via the Hazen plotting position.

    pct = 100 * (rank - 0.5) / n, with tied values sharing the average of
    their ranks. The result is strictly within (0, 100) - never exactly 0
    or 100 - so a geometric mean taken over these percentiles cannot
    collapse to 0. Higher value -> higher percentile. Empty input -> [].
    """
    n = len(values)
    if n == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda iv: iv[1])
    pctls = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based, tie-averaged
        pct = 100.0 * (avg_rank - 0.5) / n
        for k in range(i, j + 1):
            pctls[indexed[k][0]] = pct
        i = j + 1
    return pctls


def geometric_mean(values: list[float]) -> float:
    """Geometric mean (prod v)^(1/n). Assumes every value > 0; [] -> 0.0."""
    if not values:
        return 0.0
    product = 1.0
    for v in values:
        product *= v
    return product ** (1.0 / len(values))


def quartile_classes(scores: list[float]) -> list[str]:
    """Assign A/B/C/D by equal-count quartiles of `scores` (higher = worse).

    Sorted descending, the highest-scoring 25% get 'A', then 'B', 'C', 'D'.
    When n is not divisible by 4 each class holds floor(n/4) or ceil(n/4)
    members. Empty input -> [].
    """
    n = len(scores)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)
    labels = ["A", "B", "C", "D"]
    out = [""] * n
    for p, idx in enumerate(order):  # p: 0-based rank, 0 = highest score
        out[idx] = labels[min(3, p * 4 // n)]
    return out
