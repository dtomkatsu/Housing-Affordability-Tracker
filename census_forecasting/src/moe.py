"""Margin-of-error (MOE) handling for ACS estimates.

References
----------
Census Bureau, "ACS General Handbook, Chapter 7: Understanding Error and
Determining Statistical Significance" and "Chapter 8: Calculating Measures
of Error for Derived Estimates" (2018).
https://www.census.gov/content/dam/Census/library/publications/2018/acs/acs_general_handbook_2018_ch08.pdf

ACS publishes 90% margins of error. The conversion to a 1-sigma standard
error is exact under the Bureau's Gaussian assumption:

    SE = MOE / 1.645

Z=1.645 is the convention for ACS data published 2006 onward (Z=1.65 for
2005). All downstream uncertainty math in this package assumes 90% MOE
input and converts at the boundary.

Propagation formulas implemented here follow the Census Handbook
recommendations exactly. The Handbook itself warns: "These methods do not
consider the correlation or covariance between basic estimates; results
may be over- or under-estimates of the true SE." We surface that limitation
in METHODOLOGY.md rather than silently absorbing it.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

# Census MOE → SE conversion constant for 2006-onward ACS releases.
# (Z value for a 90% two-sided normal confidence interval.)
ACS_MOE_Z = 1.645


def moe_to_se(moe: float) -> float:
    """Convert a 90% margin of error to a 1-sigma standard error.

    `MOE` is taken as the half-width of the 90% CI as reported by ACS
    (always non-negative; Census uses negative MOE values to flag
    suppressed cells, which we treat as missing). Returns NaN for missing
    or sentinel-coded inputs so callers don't accidentally use a fake SE.
    """
    if moe is None or (isinstance(moe, float) and math.isnan(moe)):
        return math.nan
    if moe < 0:  # Census sentinel for suppressed/unreliable estimate
        return math.nan
    return moe / ACS_MOE_Z


def se_to_moe(se: float) -> float:
    """Inverse of `moe_to_se`. Returns the half-width of a 90% CI."""
    if se is None or (isinstance(se, float) and math.isnan(se)):
        return math.nan
    if se < 0:
        raise ValueError(f"standard error must be non-negative, got {se}")
    return se * ACS_MOE_Z


def moe_sum(moes: Iterable[float]) -> float:
    """MOE for a sum of independent ACS estimates (Handbook 8.1).

        MOE_sum = sqrt( sum_i MOE_i^2 )

    Caller must ensure components are independent (e.g. mutually
    exclusive subgroups). For overlapping or correlated components see
    the Handbook for replicate-variance methods — out of scope here.
    """
    acc = 0.0
    n = 0
    for m in moes:
        if m is None or (isinstance(m, float) and math.isnan(m)):
            return math.nan
        if m < 0:
            return math.nan
        acc += m * m
        n += 1
    if n == 0:
        return 0.0
    return math.sqrt(acc)


def moe_difference(moe_a: float, moe_b: float) -> float:
    """MOE for the difference of two independent estimates (same form as sum).

        MOE_(a-b) = sqrt(MOE_a^2 + MOE_b^2)
    """
    return moe_sum([moe_a, moe_b])


def moe_ratio(num: float, den: float, moe_num: float, moe_den: float) -> float:
    """MOE for the ratio R = num/den of two independent estimates (Handbook 8.3).

        MOE_R = (1/den) * sqrt( MOE_num^2 + R^2 * MOE_den^2 )

    Notes
    -----
    * The Handbook's *proportion* formula (when num is a strict subset of den)
      uses minus inside the radical; that's a separate routine. This is the
      general ratio of two independent estimates.
    * If `den` is zero or non-finite, returns NaN — the ratio is undefined,
      the MOE is meaningless.
    """
    if den == 0 or not math.isfinite(den):
        return math.nan
    if any(
        x is None or (isinstance(x, float) and math.isnan(x))
        for x in (num, moe_num, moe_den)
    ):
        return math.nan
    if moe_num < 0 or moe_den < 0:
        return math.nan
    R = num / den
    inside = moe_num ** 2 + (R ** 2) * (moe_den ** 2)
    return math.sqrt(inside) / abs(den)


def moe_proportion(num: float, den: float, moe_num: float, moe_den: float) -> float:
    """MOE for a proportion P = num/den when num ⊂ den (Handbook 8.2).

        MOE_P = (1/den) * sqrt( MOE_num^2 - P^2 * MOE_den^2 )

    If the value under the radical is negative, the Handbook's prescribed
    fallback is to substitute a "+" for "−" (i.e. fall back to the ratio
    formula) — implemented here.
    """
    if den == 0 or not math.isfinite(den):
        return math.nan
    if any(
        x is None or (isinstance(x, float) and math.isnan(x))
        for x in (num, moe_num, moe_den)
    ):
        return math.nan
    if moe_num < 0 or moe_den < 0:
        return math.nan
    P = num / den
    inside = moe_num ** 2 - (P ** 2) * (moe_den ** 2)
    if inside < 0:
        # Handbook 8.2: when the bracketed term is negative, use the ratio
        # formula instead. This is the official Census fallback.
        inside = moe_num ** 2 + (P ** 2) * (moe_den ** 2)
    return math.sqrt(inside) / abs(den)


def combine_se(*sigmas: float) -> float:
    """Combine independent standard errors in quadrature.

    Used to fuse sample-based SE (from ACS MOE) with model-based forecast
    SE (residual variance scaled by horizon). Independence is the
    assumption — this is the same simplification ACS itself makes for
    derived estimates and is documented as such in METHODOLOGY.md.
    """
    acc = 0.0
    for s in sigmas:
        if s is None or (isinstance(s, float) and math.isnan(s)):
            return math.nan
        if s < 0:
            return math.nan
        acc += s * s
    return math.sqrt(acc)


def ci_from_se(point: float, se: float, z: float = ACS_MOE_Z) -> tuple[float, float]:
    """Symmetric Gaussian CI for a point estimate.

    Default Z=1.645 yields a 90% interval — the same coverage ACS itself
    publishes, so projection CIs round-trip cleanly with input MOEs.
    Pass z=1.96 for a 95% interval if needed downstream.
    """
    if se is None or (isinstance(se, float) and math.isnan(se)):
        return (math.nan, math.nan)
    half = z * se
    return (point - half, point + half)


def relative_se(estimate: float, moe: float) -> float:
    """Coefficient of variation expressed as a fraction (CV = SE/|estimate|).

    The Census Bureau flags any estimate with CV > 0.40 as unreliable in
    its standard publications; CV > 0.12 is the threshold above which
    most users should treat the figure as imprecise. Returned here so the
    backtest can audit which inputs were already shaky pre-projection.
    """
    if estimate == 0 or not math.isfinite(estimate):
        return math.nan
    se = moe_to_se(moe)
    if math.isnan(se):
        return math.nan
    return se / abs(estimate)
