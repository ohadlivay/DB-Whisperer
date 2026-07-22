"""Inter-rater reliability for the Protocol 2 clarification-quality annotation.

Krippendorff's alpha, implemented from the coincidence-matrix definition so the
project keeps its tiny dependency set (no numpy/scipy). Alpha is the right
statistic here because raters may skip items: it is defined for any number of
raters with missing data, unlike a pairwise kappa.

    alpha = 1 - D_o / D_e

where D_o is the observed disagreement and D_e the disagreement expected by
chance, both built from the coincidence matrix over *pairable* values (units
rated by at least two raters). The interval metric (squared difference) suits a
1-5 Likert rating; the nominal metric is provided for completeness.

Reference values used in the tests are hand-computed in the module test, not
lifted from a library, so the implementation is checkable without one.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence


def _delta_squared(a: float, b: float, metric: str) -> float:
    if metric == "interval":
        return (a - b) ** 2
    if metric == "nominal":
        return 0.0 if a == b else 1.0
    raise ValueError(f"Unsupported metric: {metric!r}")


def krippendorff_alpha(
    units: Sequence[Sequence[float]], metric: str = "interval"
) -> float | None:
    """Alpha over ``units``; each unit is the list of scores it received.

    Missing ratings are simply absent from a unit's list, so the caller drops
    them rather than encoding a sentinel. Units with fewer than two ratings
    cannot express (dis)agreement and are ignored. Returns ``None`` — not a
    misleading number — when there is no pairable data or when every rating is
    identical (``D_e`` is zero, so reliability is undefined, not perfect).
    """
    coincidence: dict[tuple[float, float], float] = defaultdict(float)
    for ratings in units:
        m = len(ratings)
        if m < 2:
            continue
        weight = 1.0 / (m - 1)
        for i in range(m):
            for j in range(m):
                if i != j:
                    coincidence[(ratings[i], ratings[j])] += weight

    values = sorted({v for pair in coincidence for v in pair})
    if not values:
        return None

    marginals = {
        c: sum(coincidence.get((c, k), 0.0) for k in values) for c in values
    }
    n = sum(marginals.values())
    if n < 2:
        return None

    observed = sum(
        coincidence.get((c, k), 0.0) * _delta_squared(c, k, metric)
        for c in values
        for k in values
    ) / n
    expected = sum(
        marginals[c] * marginals[k] * _delta_squared(c, k, metric)
        for c in values
        for k in values
    ) / (n * (n - 1))

    if expected == 0:
        return None
    return 1.0 - observed / expected


def alpha_for_field(
    item_ratings: dict[str, dict[str, float]], metric: str = "interval"
) -> float | None:
    """Alpha from an ``{item_id: {rater: score}}`` mapping for one dimension."""
    units = [list(raters.values()) for raters in item_ratings.values()]
    return krippendorff_alpha(units, metric)
