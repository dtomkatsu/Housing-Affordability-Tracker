"""Walk-forward back-test of ACS projection methods.

Mirrors the pattern in this repo's `backtests/rent_blend_walkforward.py`
(Cleveland Fed style). For each anchor year T:

1. Truncate the series to observations with effective_year ≤ T.
2. Run each candidate model to project forward to T + h.
3. Compare against the actual ACS observation at T + h.

Metrics
-------
* MAPE — median and mean of |projected − actual| / actual.
* Bias — mean signed error / actual (positive = over-projection).
* CI coverage — fraction of folds where actual ∈ projected 90% CI.
  A well-calibrated 90% CI should contain the truth ~90% of the time.
* RMSE-pct — sqrt(mean of (pct_error)²); penalises larger errors more.

The CI coverage metric is the single most important diagnostic. Point
accuracy can look fine while a model produces dishonestly narrow CIs;
calibration > 95% means the CIs are too wide; coverage well below 90%
means the prediction interval understates actual risk.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .models import AcsObservation, ForecastPoint
from .projection import (
    project_damped_trend,
    project_ar1_log_diff,
    effective_year,
    ANNUAL_RATE_CAP,
)
from .ensemble import project_ensemble, macro_anchor_projection


@dataclass
class BacktestRow:
    geoid: str
    indicator: str
    anchor_year: int
    target_year: int
    horizon: int
    method: str
    actual: float
    projected: float
    ci90_low: float
    ci90_high: float
    sample_se: float
    forecast_se: float


@dataclass
class BacktestSummary:
    method: str
    n: int
    mean_abs_pct_error: float
    median_abs_pct_error: float
    rmse_pct: float
    bias_pct: float
    ci90_coverage: float
    rows: list[BacktestRow] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover — informational only
        return (
            f"{self.method:<22}  "
            f"n={self.n:<3} "
            f"MAPE={self.mean_abs_pct_error * 100:>6.2f}%  "
            f"medAPE={self.median_abs_pct_error * 100:>6.2f}%  "
            f"RMSEpct={self.rmse_pct * 100:>6.2f}%  "
            f"bias={self.bias_pct * 100:>+6.2f}%  "
            f"CI90cov={self.ci90_coverage * 100:>5.1f}%"
        )


# -----------------------------------------------------------------------------
# Naive baselines
# -----------------------------------------------------------------------------

def project_carry_forward(
    series_observations: Sequence[AcsObservation], target_year: int
) -> ForecastPoint | None:
    """Trivial baseline: project = latest observation. The "do nothing" rule.

    Surfaces a 90% CI built solely from the input MOE — which is the
    coverage you'd get by assuming the only uncertainty is sampling
    error and the world stops moving at the latest observation.
    """
    if not series_observations:
        return None
    latest = series_observations[-1]
    from .moe import moe_to_se, ci_from_se
    sample_se = moe_to_se(latest.moe)
    if math.isnan(sample_se):
        sample_se = 0.0
    ci_lo, ci_hi = ci_from_se(latest.estimate, sample_se)
    return ForecastPoint(
        point=latest.estimate,
        se_total=sample_se,
        se_sample=sample_se,
        se_forecast=0.0,
        ci90_low=ci_lo,
        ci90_high=ci_hi,
        method="carry_forward",
        target_year=target_year,
        geoid=latest.geoid,
        indicator=latest.indicator,
        horizon=target_year - int(effective_year(latest)),
    )


def project_linear_log(
    series_observations: Sequence[AcsObservation], target_year: int
) -> ForecastPoint | None:
    """OLS straight line in log space. Naive linear-extrapolation baseline.

    A standard "this is what a non-statistician would do" model — used in
    the back-test to ensure our richer models actually beat plain
    least-squares. Surfaces a 90% CI from the OLS residual std + sample
    SE in quadrature.
    """
    pts = [
        (effective_year(o), math.log(o.estimate))
        for o in series_observations
        if o.estimate > 0 and math.isfinite(o.estimate)
    ]
    if len(pts) < 2:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den < 1e-12:
        return None
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den
    intercept = my - slope * mx
    # Cap slope (annual log return) at the same momentum ceiling.
    slope = max(min(slope, math.log1p(ANNUAL_RATE_CAP)), math.log1p(-ANNUAL_RATE_CAP))
    log_target = intercept + slope * target_year
    point = math.exp(log_target)

    residuals = [ys[i] - (intercept + slope * xs[i]) for i in range(n)]
    if n >= 3:
        sd = math.sqrt(sum(r * r for r in residuals) / (n - 2))
    else:
        sd = abs(residuals[0])

    latest = series_observations[-1]
    horizon = target_year - effective_year(latest)
    se_forecast_log = sd * math.sqrt(max(horizon, 1.0))
    se_forecast = se_forecast_log * point

    from .moe import moe_to_se, combine_se, ci_from_se
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
        method="linear_log",
        target_year=target_year,
        geoid=latest.geoid,
        indicator=latest.indicator,
        horizon=int(round(horizon)),
    )


# -----------------------------------------------------------------------------
# Walk-forward driver
# -----------------------------------------------------------------------------

ProjectorFn = Callable[[Sequence[AcsObservation], int], "ForecastPoint | None"]


DEFAULT_METHODS: dict[str, ProjectorFn] = {
    "carry_forward": project_carry_forward,
    "linear_log": project_linear_log,
    "damped_log_trend": project_damped_trend,
    "ar1_log_diff": project_ar1_log_diff,
    "ensemble": lambda s, t: project_ensemble(s, t),
}


def truncate_to_anchor(
    series_observations: Sequence[AcsObservation], anchor_year: int
) -> list[AcsObservation]:
    """Keep only observations with effective_year ≤ anchor_year."""
    return [o for o in series_observations if effective_year(o) <= anchor_year]


def run_backtest(
    series_by_key: dict[tuple[str, str], Sequence[AcsObservation]],
    anchors: Sequence[int],
    horizon: int = 2,
    methods: dict[str, ProjectorFn] | None = None,
) -> dict[str, BacktestSummary]:
    """Run walk-forward back-test across all (geoid, indicator) series.

    Parameters
    ----------
    series_by_key : {(geoid, indicator): [AcsObservation, ...]}
        Each value is the *full* historical series; the back-test
        truncates internally per-anchor.
    anchors : list of integer anchor years.
    horizon : forecast horizon in years (default 2 — matches the
        production goal of projecting 2024 → 2026).
    methods : projector dict; defaults to `DEFAULT_METHODS`.

    Returns
    -------
    dict[method_name → BacktestSummary]
    """
    methods = methods or DEFAULT_METHODS
    rows_by_method: dict[str, list[BacktestRow]] = {m: [] for m in methods}

    for (geoid, indicator), full_series in series_by_key.items():
        full_sorted = sorted(full_series, key=lambda o: (effective_year(o), o.vintage))
        for anchor in anchors:
            train = truncate_to_anchor(full_sorted, anchor)
            if not train:
                continue
            target_year = anchor + horizon
            actual_obs = next(
                (o for o in full_sorted if effective_year(o) == target_year and o.vintage == "1y"),
                None,
            )
            if actual_obs is None or actual_obs.estimate <= 0:
                continue
            for name, fn in methods.items():
                fp = fn(train, target_year)
                if fp is None:
                    continue
                rows_by_method[name].append(BacktestRow(
                    geoid=geoid,
                    indicator=indicator,
                    anchor_year=anchor,
                    target_year=target_year,
                    horizon=horizon,
                    method=name,
                    actual=actual_obs.estimate,
                    projected=fp.point,
                    ci90_low=fp.ci90_low,
                    ci90_high=fp.ci90_high,
                    sample_se=fp.se_sample,
                    forecast_se=fp.se_forecast,
                ))

    summaries: dict[str, BacktestSummary] = {}
    for name, rows in rows_by_method.items():
        if not rows:
            summaries[name] = BacktestSummary(
                method=name, n=0,
                mean_abs_pct_error=math.nan,
                median_abs_pct_error=math.nan,
                rmse_pct=math.nan, bias_pct=math.nan,
                ci90_coverage=math.nan, rows=[],
            )
            continue
        pct_errs = [(r.projected - r.actual) / r.actual for r in rows]
        abs_pct = [abs(e) for e in pct_errs]
        mape = statistics.mean(abs_pct)
        med_ape = statistics.median(abs_pct)
        rmse_pct = math.sqrt(statistics.mean(e * e for e in pct_errs))
        bias = statistics.mean(pct_errs)
        within_ci = [
            1.0 if (r.ci90_low <= r.actual <= r.ci90_high) else 0.0
            for r in rows
        ]
        coverage = statistics.mean(within_ci)
        summaries[name] = BacktestSummary(
            method=name, n=len(rows),
            mean_abs_pct_error=mape,
            median_abs_pct_error=med_ape,
            rmse_pct=rmse_pct,
            bias_pct=bias,
            ci90_coverage=coverage,
            rows=rows,
        )
    return summaries
