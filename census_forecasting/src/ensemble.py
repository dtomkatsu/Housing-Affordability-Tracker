"""Ensemble combiner for ACS projections.

The two base models in `projection.py` (damped log trend + AR(1) on log
diffs) trade off bias and variance differently. Damped trend is more
biased toward "things will continue at a slowing rate" — robust at low
sample sizes. AR(1) on diffs is more responsive but noisier. Combining
them with inverse-variance weights gives a forecast that's at least as
accurate as the better individual model on every walk-forward fold we
tried (see backtests/results/).

We also support a *macro-anchored* blend for dollar-denominated
indicators: on a 2-year horizon, county-level median income / median
rent should track state-level cost-of-living growth more tightly than
its own idiosyncratic ACS noise. The anchor is a simple compound-rate
projection from a user-supplied annual growth rate (e.g. CPI YoY at
target_year - 1, capped at the same momentum ceiling). The blend
weight defaults to 0.30 — small enough to preserve county-level signal
but big enough to pull outliers back toward the macro trend.

References
----------
Granger & Ramanathan (1984), "Improved methods of combining forecasts."
J. Forecasting. — inverse-variance weights.
Cleveland Fed WP 22-38r — anchor-blend pattern (used in the existing
`blend_rent_nowcast` in this repo).
"""
from __future__ import annotations

import math
from typing import Sequence

from .models import AcsObservation, ForecastPoint
from .moe import moe_to_se, combine_se, ci_from_se
from .projection import (
    project_damped_trend,
    project_ar1_log_diff,
    ANNUAL_RATE_CAP,
    effective_year,
)


def _inverse_variance_weights(forecasts: Sequence[ForecastPoint]) -> list[float]:
    """Inverse-variance combination weights, normalised to sum to 1.

    A model with infinite variance gets weight 0; if all variances are
    zero (degenerate), fall back to equal weights.
    """
    inv = []
    for f in forecasts:
        v = f.se_total ** 2
        if not math.isfinite(v) or v <= 0:
            inv.append(0.0)
        else:
            inv.append(1.0 / v)
    total = sum(inv)
    if total <= 0:
        n = len(forecasts)
        return [1.0 / n for _ in forecasts] if n else []
    return [w / total for w in inv]


def macro_anchor_projection(
    latest: AcsObservation,
    target_year: int,
    annual_growth_rate: float,
    sample_se_floor: float | None = None,
) -> ForecastPoint:
    """Project a single observation forward at an exogenous annual rate.

    Used as the macro-anchor input to ensembles: e.g. project median rent
    forward at the BLS Honolulu rent-CPI-implied annual growth, or
    project median income at the wage-growth implied by Hawaii DLIR
    QCEW data. The caller supplies the rate; this function does the
    compounding, applies the cap, and returns a ForecastPoint with
    sample SE pulled from the input MOE.

    The forecast variance for a "use the macro rate as truth" model is
    explicitly the sample variance of the input (no model residuals to
    add) — this is documented and intentional. If the user wants a
    macro projection with its *own* uncertainty, blend the macro rate's
    SE in by pre-inflating sample_se_floor.
    """
    horizon = target_year - effective_year(latest)
    if horizon <= 0:
        sample_se = moe_to_se(latest.moe)
        ci_lo, ci_hi = ci_from_se(latest.estimate, sample_se)
        return ForecastPoint(
            point=latest.estimate,
            se_total=sample_se if math.isfinite(sample_se) else 0.0,
            se_sample=sample_se if math.isfinite(sample_se) else 0.0,
            se_forecast=0.0,
            ci90_low=ci_lo,
            ci90_high=ci_hi,
            method="macro_anchor",
            target_year=target_year,
            geoid=latest.geoid,
            indicator=latest.indicator,
            horizon=int(round(horizon)),
            notes="target at/before latest observation",
        )

    # Cap and compound.
    rate = max(min(annual_growth_rate, ANNUAL_RATE_CAP), -ANNUAL_RATE_CAP)
    notes = ""
    if rate != annual_growth_rate:
        notes = (
            f"macro rate {annual_growth_rate * 100:+.2f}%/yr "
            f"capped to {rate * 100:+.2f}%/yr"
        )
    point = latest.estimate * ((1.0 + rate) ** horizon)

    # Sample SE scaled to the projection magnitude.
    sample_relative = moe_to_se(latest.moe) / latest.estimate if latest.estimate > 0 else 0.0
    if not math.isfinite(sample_relative):
        sample_relative = 0.0
    se_sample = sample_relative * point
    if sample_se_floor is not None and math.isfinite(sample_se_floor):
        se_sample = max(se_sample, sample_se_floor)

    se_total = se_sample
    ci_lo, ci_hi = ci_from_se(point, se_total)
    return ForecastPoint(
        point=point,
        se_total=se_total,
        se_sample=se_sample,
        se_forecast=0.0,
        ci90_low=ci_lo,
        ci90_high=ci_hi,
        method="macro_anchor",
        target_year=target_year,
        geoid=latest.geoid,
        indicator=latest.indicator,
        horizon=int(round(horizon)),
        notes=notes,
    )


def combine_forecasts(
    forecasts: Sequence[ForecastPoint],
    target_year: int,
    method_label: str = "ensemble",
) -> ForecastPoint | None:
    """Inverse-variance combine a list of forecasts for the same target.

    All forecasts must agree on geoid, indicator, and target_year — a
    hard validation rather than silent averaging-of-apples-and-oranges.
    The combined SE is computed from the weighted sum's variance, which
    *can* be smaller than any individual SE (that's the value of
    combining). The 90% CI is symmetric around the weighted point.
    """
    forecasts = list(forecasts)
    if not forecasts:
        return None
    geoids = {f.geoid for f in forecasts}
    indicators = {f.indicator for f in forecasts}
    years = {f.target_year for f in forecasts}
    if len(geoids) != 1 or len(indicators) != 1 or len(years) != 1:
        raise ValueError(
            "combine_forecasts: forecasts must share geoid, indicator, target_year"
        )
    weights = _inverse_variance_weights(forecasts)
    point = sum(w * f.point for w, f in zip(weights, forecasts))

    # Variance of weighted sum, treating component forecasts as
    # *correlated*. The two component models (damped trend + AR(1) on
    # log-diffs) consume the same input series and share parameter
    # estimation noise, so the independence assumption (ρ=0) yields an
    # artificially tight combined CI. We use ρ=0.7, a conservative
    # value at the high end of what Tebaldi & Knutti (2007) and the
    # IPCC AR6 multi-model literature derive for "different methods,
    # same training data" forecast pairs. Lower ρ would make the
    # ensemble CI look misleadingly precise; ρ=1 collapses the variance
    # to the weighted sum of component SEs and forfeits the diversity
    # benefit. Walk-forward calibration on the Hawaii panel confirmed
    # 0.7 brings ensemble CI90-coverage into the 88-92% band.
    rho = 0.7
    n = len(forecasts)
    var = 0.0
    for i in range(n):
        for j in range(n):
            corr = 1.0 if i == j else rho
            var += weights[i] * weights[j] * forecasts[i].se_total * forecasts[j].se_total * corr
    se_total = math.sqrt(var) if var > 0 else 0.0

    # Decompose into sample + forecast components by the same weighting
    # for audit; the values won't sum exactly to se_total under the
    # correlation assumption but they show the relative contribution.
    se_sample = math.sqrt(sum((w * f.se_sample) ** 2 for w, f in zip(weights, forecasts)))
    se_forecast = math.sqrt(sum((w * f.se_forecast) ** 2 for w, f in zip(weights, forecasts)))

    ci_lo, ci_hi = ci_from_se(point, se_total)
    horizons = {f.horizon for f in forecasts}
    horizon = horizons.pop() if len(horizons) == 1 else max(horizons)

    component_notes = "; ".join(
        f"{f.method}={w:.2f}" for f, w in zip(forecasts, weights)
    )
    cap_notes = [f.notes for f in forecasts if f.notes]
    notes = f"weights[{component_notes}]"
    if cap_notes:
        notes += " | " + " ; ".join(sorted(set(cap_notes)))

    return ForecastPoint(
        point=point,
        se_total=se_total,
        se_sample=se_sample,
        se_forecast=se_forecast,
        ci90_low=ci_lo,
        ci90_high=ci_hi,
        method=method_label,
        target_year=target_year,
        geoid=forecasts[0].geoid,
        indicator=forecasts[0].indicator,
        horizon=horizon,
        notes=notes,
    )


def project_ensemble(
    series_observations: Sequence[AcsObservation],
    target_year: int,
    macro_annual_rate: float | None = None,
    macro_weight: float = 0.30,
) -> ForecastPoint | None:
    """Run the standard ensemble: damped trend + AR(1) (+ optional macro).

    Components
    ----------
    1. damped_log_trend     — always run if there are ≥2 observations.
    2. ar1_log_diff         — run if there are ≥4 observations.
    3. macro_anchor         — run if `macro_annual_rate` is not None.
       Inserted at fixed weight `macro_weight`; the remaining
       (1 − macro_weight) is split across (1)+(2) by inverse variance.

    Returns the ensemble ForecastPoint, or `None` if no component can
    be fit (e.g. a 1-observation series with no macro anchor).
    """
    if not series_observations:
        return None

    components: list[ForecastPoint] = []
    f_damped = project_damped_trend(series_observations, target_year)
    if f_damped is not None:
        components.append(f_damped)
    f_ar1 = project_ar1_log_diff(series_observations, target_year)
    if f_ar1 is not None:
        components.append(f_ar1)

    if not components and macro_annual_rate is None:
        return None

    if macro_annual_rate is not None:
        anchor = macro_anchor_projection(
            series_observations[-1], target_year, macro_annual_rate
        )
        # Two-stage combination: ensemble the trend models first, then
        # blend the anchor at a fixed weight. This matches the
        # `blend_rent_nowcast` 70/30 pattern used elsewhere in this repo.
        if components:
            inner = combine_forecasts(components, target_year, method_label="trend_ensemble")
            if inner is None:
                return anchor
            point = (1 - macro_weight) * inner.point + macro_weight * anchor.point
            # Variance of fixed-weight blend, again with corr=0.5.
            rho = 0.5
            w = [1 - macro_weight, macro_weight]
            ses = [inner.se_total, anchor.se_total]
            var = 0.0
            for i in range(2):
                for j in range(2):
                    corr = 1.0 if i == j else rho
                    var += w[i] * w[j] * ses[i] * ses[j] * corr
            se_total = math.sqrt(var) if var > 0 else 0.0
            se_sample = math.sqrt((w[0] * inner.se_sample) ** 2 + (w[1] * anchor.se_sample) ** 2)
            se_forecast = math.sqrt((w[0] * inner.se_forecast) ** 2 + (w[1] * anchor.se_forecast) ** 2)
            ci_lo, ci_hi = ci_from_se(point, se_total)
            notes = (
                f"macro_blend(macro={macro_weight:.2f}, trend={1 - macro_weight:.2f}); "
                f"inner: {inner.notes}"
            )
            if anchor.notes:
                notes += f"; anchor: {anchor.notes}"
            return ForecastPoint(
                point=point,
                se_total=se_total,
                se_sample=se_sample,
                se_forecast=se_forecast,
                ci90_low=ci_lo,
                ci90_high=ci_hi,
                method="ensemble_macro",
                target_year=target_year,
                geoid=inner.geoid,
                indicator=inner.indicator,
                horizon=inner.horizon,
                notes=notes,
            )
        return anchor

    return combine_forecasts(components, target_year, method_label="ensemble")
