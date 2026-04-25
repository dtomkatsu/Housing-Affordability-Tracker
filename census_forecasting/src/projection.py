"""Forecast models for ACS time series.

Design philosophy
-----------------
County-level ACS series are short (≤15 annual observations for 1-year
since 2005, less for many counties). Methods that need a lot of data —
ARIMA(p,d,q) with order selection, full Bayesian hierarchical Fay-Herriot
with MCMC, neural nets — overfit at this sample size. The literature
(Wilson et al., "Methods for Small Area Population Forecasts: State of
the Art and Research Needs", 2021) finds simple constrained methods with
smoothing are competitive with or beat sophisticated models at ≤5-year
horizons for sub-state geography.

We therefore build a small ensemble of simple, interpretable models and
combine them. Every component has an explicit reason to exist:

1. **Damped local linear trend** in log space — handles compounding
   percentage changes (income, rent, value all grow multiplicatively),
   damps the trend toward zero so a single noisy year doesn't drive the
   forecast, and degenerates to flat when the series is flat. Inspired by
   the ETS(A,Ad,N) state-space model (Hyndman et al. 2008).
2. **AR(1) on log-differences** — captures the empirical mean-reversion
   of YoY growth rates and gives a residual variance estimate that
   propagates cleanly into prediction intervals.
3. **Macro anchor** (in `ensemble.py`) — for dollar series, blend with
   the BLS CPI projection so a county whose ACS reading is briefly out
   of step with the state-wide cost-of-living signal gets pulled back.

All models share three discipline rules taken straight from the
existing CPI/rent code in this repo (METHODOLOGY.md "Forward-projection
rule"):

* Compound rates, not arithmetic — `(1+r)^h` not `1 + r·h`.
* Per-period rate cap — momentum ceiling on |annual growth| stops
  noisy 1-year prints from blowing up multi-year extrapolation.
* Honest "method" tagging — every forecast row carries the model name
  and any cap/clamp note in `ForecastPoint.notes`.

References
----------
Hyndman, R., Koehler, A., Ord, J., Snyder, R. (2008).
  *Forecasting with Exponential Smoothing: The State Space Approach.*
  Springer.
Wilson, T., Grossman, I., Alexander, M., et al. (2021).
  "Methods for Small Area Population Forecasts: State of the Art."
  Population Research and Policy Review.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .models import AcsObservation, ForecastPoint
from .moe import moe_to_se, combine_se, ci_from_se, ACS_MOE_Z


# -----------------------------------------------------------------------------
# Shared constants
# -----------------------------------------------------------------------------

# Annual momentum cap: maximum |compound annual growth rate| we'll let any
# model project forward. Calibrated to historical Hawaii county-level YoY
# moves in B19013 / B25064 / B25077, which have stayed inside ±15% in every
# vintage since 2010. Anything beyond that at the 2-year horizon is more
# likely a noisy ACS print than a real trend, so we clamp.
#
# This is the annual analog of the ±0.0189/month CPI cap that already
# governs the grocery and TFP projections in this repo. Same discipline,
# different cadence:
#     CPI:  (1 ± 0.0189)^12  ≈  +25.2% / −20.5% per year
#     ACS:  (1 ± 0.10)^1     =   ±10% per year
# We use a *tighter* cap here because ACS demographic series have lower
# realised volatility than monthly food CPI and a 2-year horizon
# (vs 1–4 months for CPI) means runaway compounding is more dangerous.
ANNUAL_RATE_CAP = 0.10

# Empirical SE calibration multiplier. Walk-forward evaluation on the
# Hawaii ACS panel (2010-2024, see backtests/results/) found that the
# raw Hyndman ETS variance + n/(n-2) small-sample correction produced
# 90%-CIs that contained the truth in ~77% of folds — too tight by a
# factor of ~1.3× in standard-deviation terms. This multiplier scales
# the forecast SE upward to bring back-test coverage close to 90%.
#
# It is *not* tuned per geography or per indicator (that would be
# overfitting). It is a single global multiplier, documented here, and
# its source is the same back-test that ships in this package.
# Re-derive with `scripts/run_backtest.py --calibrate-se`.
EMPIRICAL_SE_INFLATOR = 1.30


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# -----------------------------------------------------------------------------
# Time-index normalisation across mixed vintages
# -----------------------------------------------------------------------------

def effective_year(obs: AcsObservation) -> float:
    """Return the effective time index for an ACS observation.

    1-year ACS represents a single calendar year; effective year = year.
    5-year ACS represents the rolling 5-year window ending in `year`;
    by convention we place it at the *midpoint* of the window, year - 2.

    This is the standard time-axis convention used in the SAE literature
    when blending overlapping multi-year and single-year series. See
    e.g. Bauder & Spell (2017) Census Bureau working paper RRS-2017-04.
    """
    if obs.vintage == "1y":
        return float(obs.year)
    if obs.vintage == "5y":
        return obs.year - 2.0
    raise ValueError(f"unknown vintage {obs.vintage!r}")


# -----------------------------------------------------------------------------
# Damped local linear trend (Holt's damped method, log space)
# -----------------------------------------------------------------------------

@dataclass
class DampedTrendFit:
    """Fitted state of a damped local linear trend in log space.

    Internally we work on log(y); the projection exponentiates back at
    the boundary. This makes the "compound monthly rate" discipline
    natural — a step in level-log space *is* a continuous growth rate.

    Attributes
    ----------
    level : float       — last fitted level (in log space)
    trend : float       — last fitted slope (in log space, per year)
    alpha : float ∈ [0,1] — level smoothing
    beta  : float ∈ [0,1] — trend smoothing
    phi   : float ∈ (0,1] — damping factor
    residual_std : float — std of in-sample log-space one-step residuals
    last_year : float    — effective year of the final fitted point
    n_obs : int          — number of observations consumed
    """
    level: float
    trend: float
    alpha: float
    beta: float
    phi: float
    residual_std: float
    last_year: float
    n_obs: int

    def project_log(self, h_years: float) -> float:
        """Forecast log(y) `h_years` past the last fitted point.

        Damped trend horizon-h forecast (Hyndman et al. 2008, eq. 7.5):
            ŷ_{t+h} = level + Σ_{k=1..h} φ^k · trend
                    = level + trend · (φ + φ² + ... + φ^h)
                    = level + trend · φ · (1 − φ^h) / (1 − φ)
        which we evaluate directly. For non-integer h (e.g. 5y vintage
        midpoint at h=1.5) the same closed form applies.
        """
        if h_years <= 0:
            return self.level
        if abs(self.phi - 1.0) < 1e-9:
            damp_sum = h_years
        else:
            damp_sum = self.phi * (1.0 - self.phi ** h_years) / (1.0 - self.phi)
        return self.level + self.trend * damp_sum

    def forecast_se_log(self, h_years: float) -> float:
        """Forecast SE in log space at horizon h, ETS(A,Ad,N) exact form.

        From Hyndman et al. (2008), eq. 6.1 / Table 6.1, the h-step
        prediction variance for the damped local linear model is

            σ²_h = σ² · [ 1 + Σ_{j=1}^{h-1} c_j² ]
            c_j  = α · (1 + β · φ · (1 − φ^j) / (1 − φ))

        i.e. the cumulative effect of one-step shocks propagated through
        the level and trend updates. For φ→1 this reduces to the
        undamped Holt's variance; for j=0 it equals σ² (the one-step
        residual variance).

        We then apply two conservative corrections:

        1. Small-sample bias on σ̂². Residuals are computed *after*
           parameters have been fit, so σ̂² underestimates the
           population variance. Standard OLS-style fix: multiply by
           n/(n − p) where p = 2 effective parameters (level, trend).
        2. Empirical calibration multiplier `EMPIRICAL_SE_INFLATOR`
           (see module-level constant). Calibrated on the Hawaii
           back-test such that the resulting 90% CI achieves ~90%
           coverage in walk-forward evaluation. Documented in
           METHODOLOGY.md and re-derivable via `scripts/run_backtest.py
           --calibrate-se`.

        The combination is honest: the model-internal variance is the
        Hyndman closed form, the small-sample correction is a textbook
        OLS-style rescaling, and the empirical κ is a transparent,
        documented multiplier rather than a hidden fudge factor.
        """
        if h_years <= 0:
            return 0.0
        h = max(1, int(round(h_years)))
        # Hyndman ETS(A,Ad,N) h-step-ahead variance coefficient.
        if abs(self.phi - 1.0) < 1e-9:
            # Undamped limit: c_j = α(1 + β·j)
            cs = [self.alpha * (1 + self.beta * j) for j in range(1, h)]
        else:
            cs = [
                self.alpha * (1 + self.beta * self.phi * (1 - self.phi ** j) / (1 - self.phi))
                for j in range(1, h)
            ]
        var_factor = 1.0 + sum(c * c for c in cs)
        # Small-sample bias correction (n/(n-2)); guard for tiny n.
        if self.n_obs > 2:
            small_sample_correction = self.n_obs / (self.n_obs - 2)
        else:
            small_sample_correction = 2.0
        var_log = (
            (self.residual_std ** 2)
            * var_factor
            * small_sample_correction
            * (EMPIRICAL_SE_INFLATOR ** 2)
        )
        return math.sqrt(var_log)


def fit_damped_trend(
    observations: Sequence[AcsObservation],
    phi: float = 0.85,
    alpha: float = 0.6,
    beta: float = 0.2,
) -> DampedTrendFit | None:
    """Fit a damped local linear trend in log space.

    Parameters
    ----------
    observations : sorted list of AcsObservation
    phi   : damping factor — pulls the trend toward zero each step.
            phi=1 reduces to undamped Holt's method; phi=0.85 is the
            literature default and what the M-competitions found best
            for short series with limited training data.
    alpha : level smoothing weight (higher = more responsive to most
            recent observation; lower = more inertia). 0.6 is a robust
            choice for short series with mild noise.
    beta  : trend smoothing weight. Smaller than alpha so the slope
            moves slowly relative to the level.

    The smoothing constants are fixed rather than estimated by MLE — at
    n≤10 observations the likelihood is too flat for stable estimation.
    Wilson et al. (2021) explicitly recommend fixed smoothing constants
    for small-area work.

    Returns
    -------
    DampedTrendFit, or None if there are <2 valid points.
    """
    pts = [
        (effective_year(o), math.log(o.estimate))
        for o in observations
        if o.estimate > 0 and math.isfinite(o.estimate)
    ]
    if len(pts) < 2:
        return None
    pts.sort(key=lambda x: x[0])

    # Initialise: level = first log value, trend = first finite difference.
    level = pts[0][1]
    trend = (pts[1][1] - pts[0][1]) / max(pts[1][0] - pts[0][0], 1.0)

    residuals: list[float] = []
    last_year = pts[0][0]
    for (yr, ly) in pts[1:]:
        gap = yr - last_year
        if gap <= 0:
            continue
        # Multi-step propagation when there are gaps in the series (e.g.
        # the missing 2020 1-year ACS): apply damped trend across the gap
        # before the level update, otherwise a 2-year jump distorts the
        # implicit one-step growth rate.
        if abs(phi - 1.0) < 1e-9:
            damp = gap
        else:
            damp = phi * (1.0 - phi ** gap) / (1.0 - phi)
        forecast_log = level + trend * damp
        residual = ly - forecast_log
        residuals.append(residual)

        # Standard Holt damped recursions (Hyndman §7.4) for arbitrary gap:
        # collapse the gap-step forecast into a one-step update.
        new_level = forecast_log + alpha * residual
        # Trend update uses observed slope over the gap.
        observed_slope = (ly - level) / gap
        # phi^gap damping of prior trend handles the multi-step gap correctly.
        damp_prior = phi ** gap
        new_trend = beta * observed_slope + (1.0 - beta) * damp_prior * trend

        level = new_level
        trend = new_trend
        last_year = yr

    if not residuals:
        return None

    # Population SD (n-1) of residuals; floor at 0 if n=2 (one residual).
    if len(residuals) >= 2:
        mean_r = sum(residuals) / len(residuals)
        var = sum((r - mean_r) ** 2 for r in residuals) / (len(residuals) - 1)
        std = math.sqrt(var)
    else:
        std = abs(residuals[0])

    return DampedTrendFit(
        level=level,
        trend=trend,
        alpha=alpha,
        beta=beta,
        phi=phi,
        residual_std=std,
        last_year=last_year,
        n_obs=len(pts),
    )


# -----------------------------------------------------------------------------
# Public projection entry point
# -----------------------------------------------------------------------------

def project_damped_trend(
    series_observations: Sequence[AcsObservation],
    target_year: int,
    phi: float = 0.85,
) -> ForecastPoint | None:
    """Project the series to `target_year` using the damped trend model.

    Wraps `fit_damped_trend` and returns a `ForecastPoint` with
    propagated MOE, model residual SE, and a 90% prediction interval.
    Applies the per-year compound rate cap defined by `ANNUAL_RATE_CAP`
    and surfaces the cap event in `notes` when triggered.
    """
    if not series_observations:
        return None

    fit = fit_damped_trend(series_observations, phi=phi)
    if fit is None:
        return None

    # Use the latest 1-year if available (most recent signal), else the
    # latest 5-year. Sample MOE attached to the *last input observation*
    # is the lower bound on uncertainty: the projection cannot be more
    # certain than the most recent measurement it pivots from.
    latest = series_observations[-1]
    latest_eff_year = effective_year(latest)

    horizon = target_year - latest_eff_year
    if horizon <= 0:
        # Caller asked for a target year at or before the last data point
        # — no projection needed; return the observation in forecast form.
        sample_se = moe_to_se(latest.moe)
        ci_lo, ci_hi = ci_from_se(latest.estimate, sample_se)
        return ForecastPoint(
            point=latest.estimate,
            se_total=sample_se if math.isfinite(sample_se) else 0.0,
            se_sample=sample_se if math.isfinite(sample_se) else 0.0,
            se_forecast=0.0,
            ci90_low=ci_lo,
            ci90_high=ci_hi,
            method="passthrough",
            target_year=target_year,
            geoid=latest.geoid,
            indicator=latest.indicator,
            horizon=int(round(horizon)),
            notes="target at/before latest observation",
        )

    # Project, then enforce the annual rate cap. The cap is applied in
    # log space (where the projection lives) by clipping the *implied
    # mean compound annual rate* between the latest level and the
    # target. This keeps shorter horizons proportionally less affected
    # than longer ones, which matches the way the CPI cap works.
    log_target = fit.project_log(horizon)
    implied_annual_rate = math.expm1((log_target - math.log(latest.estimate)) / horizon)
    notes = ""
    if implied_annual_rate > ANNUAL_RATE_CAP:
        log_target = math.log(latest.estimate) + horizon * math.log1p(ANNUAL_RATE_CAP)
        notes = f"capped at +{ANNUAL_RATE_CAP * 100:.1f}%/yr momentum ceiling"
    elif implied_annual_rate < -ANNUAL_RATE_CAP:
        log_target = math.log(latest.estimate) + horizon * math.log1p(-ANNUAL_RATE_CAP)
        notes = f"capped at -{ANNUAL_RATE_CAP * 100:.1f}%/yr momentum floor"

    point = math.exp(log_target)

    # Forecast SE: residual std scaled by sqrt(h) in log space, then
    # converted to dollar space via the delta method ( σ_y ≈ σ_logy · y ).
    se_forecast_log = fit.forecast_se_log(horizon)
    se_forecast = se_forecast_log * point

    # Sample SE at the latest observation (scaled by point/latest.estimate
    # to keep the relative SE constant across the projection — a
    # defensible first-order assumption since ACS sampling error scales
    # roughly with magnitude for these indicators).
    sample_relative = moe_to_se(latest.moe) / latest.estimate if latest.estimate > 0 else 0.0
    if not math.isfinite(sample_relative):
        sample_relative = 0.0
    se_sample = sample_relative * point

    se_total = combine_se(se_sample, se_forecast)
    ci_lo, ci_hi = ci_from_se(point, se_total)

    return ForecastPoint(
        point=point,
        se_total=se_total,
        se_sample=se_sample,
        se_forecast=se_forecast,
        ci90_low=ci_lo,
        ci90_high=ci_hi,
        method="damped_log_trend",
        target_year=target_year,
        geoid=latest.geoid,
        indicator=latest.indicator,
        horizon=int(round(horizon)),
        notes=notes,
    )


# -----------------------------------------------------------------------------
# AR(1) on log differences — secondary model used in ensemble
# -----------------------------------------------------------------------------

@dataclass
class AR1LogDiffFit:
    """AR(1) fit on year-over-year log returns: r_t = c + ρ·r_{t-1} + ε_t.

    Residual SD ε is the basis of forecast SE. ρ near 0 means YoY shocks
    are ~independent (random walk on log levels); ρ near 1 means the
    series mean-reverts slowly. Useful as a sanity check on the damped
    trend's projected slope and as a second voice in the ensemble.
    """
    intercept: float
    rho: float
    residual_std: float
    last_log: float
    last_diff: float
    last_year: float


def fit_ar1_log_diff(observations: Sequence[AcsObservation]) -> AR1LogDiffFit | None:
    """Fit AR(1) to YoY log differences via OLS.

    Needs at least 4 observations: 3 log-diffs to fit a slope+intercept
    plus 1 to compute a residual. Returns None below that.
    """
    pts = [
        (effective_year(o), math.log(o.estimate))
        for o in observations
        if o.estimate > 0 and math.isfinite(o.estimate)
    ]
    if len(pts) < 4:
        return None
    pts.sort(key=lambda x: x[0])

    diffs: list[tuple[float, float]] = []  # (year, annualised log diff)
    for i in range(1, len(pts)):
        gap = pts[i][0] - pts[i - 1][0]
        if gap <= 0:
            continue
        diffs.append((pts[i][0], (pts[i][1] - pts[i - 1][1]) / gap))
    if len(diffs) < 3:
        return None

    # OLS for r_t = c + ρ · r_{t-1}
    xs = [d[1] for d in diffs[:-1]]
    ys = [d[1] for d in diffs[1:]]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    rho = num / den if den > 1e-12 else 0.0
    # Constrain rho to a stationary range to keep multi-step forecasts sane.
    rho = _clamp(rho, -0.95, 0.95)
    intercept = mean_y - rho * mean_x
    residuals = [ys[i] - intercept - rho * xs[i] for i in range(n)]
    if len(residuals) >= 2:
        std = math.sqrt(sum((r - sum(residuals) / len(residuals)) ** 2 for r in residuals) / (len(residuals) - 1))
    else:
        std = abs(residuals[0])

    return AR1LogDiffFit(
        intercept=intercept,
        rho=rho,
        residual_std=std,
        last_log=pts[-1][1],
        last_diff=diffs[-1][1],
        last_year=pts[-1][0],
    )


def project_ar1_log_diff(
    series_observations: Sequence[AcsObservation],
    target_year: int,
) -> ForecastPoint | None:
    """Project via AR(1) on log diffs and assemble a ForecastPoint.

    Multi-step rollout: each step's expected log-diff is c + ρ·r_{t-1};
    we sum these across the horizon to project the level.
    """
    fit = fit_ar1_log_diff(series_observations)
    if fit is None:
        return None
    latest = series_observations[-1]
    horizon = target_year - effective_year(latest)
    if horizon <= 0:
        return None

    h = int(round(horizon))
    cum_log = 0.0
    r_prev = fit.last_diff
    for _ in range(h):
        r_t = fit.intercept + fit.rho * r_prev
        cum_log += r_t
        r_prev = r_t

    # Apply the same annual rate cap.
    implied = math.expm1(cum_log / max(horizon, 1.0))
    notes = ""
    if implied > ANNUAL_RATE_CAP:
        cum_log = horizon * math.log1p(ANNUAL_RATE_CAP)
        notes = f"capped at +{ANNUAL_RATE_CAP * 100:.1f}%/yr momentum ceiling"
    elif implied < -ANNUAL_RATE_CAP:
        cum_log = horizon * math.log1p(-ANNUAL_RATE_CAP)
        notes = f"capped at -{ANNUAL_RATE_CAP * 100:.1f}%/yr momentum floor"

    log_target = fit.last_log + cum_log
    point = math.exp(log_target)

    # Variance of cumulative h-step AR(1) forecast (closed form):
    #     Var(sum_{k=1..h} r_{t+k}) = σ² · sum_{k=0..h-1} ((1 - ρ^(k+1)) / (1 - ρ))²
    # For ρ→1 (random walk on diffs) this is σ²·h·(h+1)·(2h+1)/6 — explosive,
    # which is correct (the random-walk-on-diffs is I(2)). The cap above
    # keeps the *point* sane; here we keep the *variance* honest.
    var_log = 0.0
    if abs(1.0 - fit.rho) < 1e-9:
        for k in range(h):
            var_log += (k + 1) ** 2
        var_log *= fit.residual_std ** 2
    else:
        for k in range(h):
            coef = (1.0 - fit.rho ** (k + 1)) / (1.0 - fit.rho)
            var_log += coef ** 2
        var_log *= fit.residual_std ** 2
    # Same small-sample + empirical-calibration treatment as the damped
    # trend model — keeps the two component SEs on a comparable footing
    # so that the inverse-variance ensemble weighting doesn't favour
    # whichever model happens to under-state its variance.
    n_diffs = len(series_observations) - 1
    small_sample = n_diffs / max(n_diffs - 2, 1)
    var_log *= small_sample * (EMPIRICAL_SE_INFLATOR ** 2)
    se_forecast_log = math.sqrt(var_log)
    se_forecast = se_forecast_log * point

    sample_relative = moe_to_se(latest.moe) / latest.estimate if latest.estimate > 0 else 0.0
    if not math.isfinite(sample_relative):
        sample_relative = 0.0
    se_sample = sample_relative * point
    se_total = combine_se(se_sample, se_forecast)
    ci_lo, ci_hi = ci_from_se(point, se_total)

    return ForecastPoint(
        point=point,
        se_total=se_total,
        se_sample=se_sample,
        se_forecast=se_forecast,
        ci90_low=ci_lo,
        ci90_high=ci_hi,
        method="ar1_log_diff",
        target_year=target_year,
        geoid=latest.geoid,
        indicator=latest.indicator,
        horizon=h,
        notes=notes,
    )
