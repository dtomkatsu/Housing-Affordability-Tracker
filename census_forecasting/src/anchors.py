"""Multi-source macro-anchor ensemble.

Replaces the legacy fixed-weight (0.30 to a single CPI rate) macro
blend with a weighted ensemble across all sources admissible for the
indicator under projection.

How weights are determined
--------------------------
For each `(indicator, anchor_source)` pair we compute the historical
RMSE of the source's smoothed YoY log-rate as an estimator of the
*subsequent* ACS YoY change at the same horizon. Sources that closely
track ACS get higher weight; sources that are noisy or biased get
lower. Weights are inverse-variance:

        w_i ∝ 1 / (RMSE_i² + σ_floor²)

with `σ_floor` preventing a single near-perfect source from monopolising
the weight (which would defeat the diversification benefit).

Calibration data
----------------
Per-(indicator, source) RMSE is precomputed by
`scripts/calibrate_anchors.py` and stored in
`data/anchors/calibration.json`. If the calibration file is missing
(first run), the loader falls back to equal weights and emits a
warning. The production `project_acs_2026.py` script always runs
calibration first and writes the file.

Hidden-data discipline
----------------------
The calibration uses only data that would have been visible at each
back-test anchor year T, via `AnchorSource.publication_lag_years`. The
production projection at run time T_now uses *current* visible data;
we explicitly do NOT condition the weights on the projection target.
That would constitute leak-back of out-of-sample information.

References
----------
Granger & Ramanathan (1984) on inverse-variance combination.
Bates & Granger (1969) on the variance reduction available to combinable
forecasts even when they share underlying signal.
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .models import AcsObservation, ForecastPoint
from .moe import moe_to_se, ci_from_se, combine_se
from .projection import (
    ANNUAL_RATE_CAP,
    effective_year,
    EMPIRICAL_SE_INFLATOR,
)
from .sources import AnchorSource, AnnualRate, available_sources


_PKG_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALIBRATION_PATH = _PKG_ROOT / "data" / "anchors" / "calibration.json"


# -----------------------------------------------------------------------------
# Anchor-rate aggregator
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class AnchorRate:
    """Combined annual log-rate from a set of macro anchors.

    `point_log_rate` is the inverse-variance-weighted mean.
    `se_log_rate` is the SE of that weighted mean (variance of
    weighted sum, accounting for cross-source correlation rho).
    `components` is the per-source contribution table for audit.
    """
    point_log_rate: float
    se_log_rate: float
    indicator: str
    end_year: int
    components: list[tuple[str, float, float, float]]  # (source, log_rate, se, weight)


def _inverse_variance_weights(
    source_names: Sequence[str],
    rates: Sequence[AnnualRate],
    rmse_floor: float = 0.005,
    indicator_rmse: Optional[dict[str, float]] = None,
) -> list[float]:
    """Inverse-(variance + floor²) weighting across source/rate pairs.

    If `indicator_rmse` is provided (per-source RMSE from calibration),
    use it as the variance component; otherwise fall back to each
    source's empirical SE on its own rate series. Floor prevents any
    single source from monopolising the ensemble weight.
    """
    if len(source_names) != len(rates):
        raise ValueError("source_names and rates must align")
    raw_inv: list[float] = []
    for name, r in zip(source_names, rates):
        if indicator_rmse is not None and name in indicator_rmse:
            v = max(indicator_rmse[name], rmse_floor) ** 2
        else:
            v = max(r.se_log_rate, rmse_floor) ** 2
        if not math.isfinite(v) or v <= 0:
            raw_inv.append(0.0)
        else:
            raw_inv.append(1.0 / v)
    total = sum(raw_inv)
    if total <= 0:
        n = len(rates)
        return [1.0 / n for _ in rates] if n else []
    return [w / total for w in raw_inv]


def load_calibration(
    path: Path = DEFAULT_CALIBRATION_PATH,
) -> Optional[dict]:
    """Load the full calibration payload.

    Returns the entire JSON object (not just one slice) so callers can
    use both `rmse_by_indicator_source` (per-source weights), and
    `rmse_by_indicator_method` + `se_inflator_override_by_indicator_method`
    (macro/trend blend weight + SE rescale).

    Returns None if the file is absent or unreadable. The projection
    code falls back to equal weights and the legacy 0.30 macro weight
    when None.
    """
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[anchors] calibration load failed ({exc}); falling back to equal weights",
              file=sys.stderr)
        return None


def combined_anchor_rate(
    indicator: str,
    end_year: int,
    sources: Optional[Sequence[AnchorSource]] = None,
    calibration: Optional[dict[str, dict[str, float]]] = None,
    correlation_rho: float = 0.6,
    calibration_horizon: int = 2,
) -> Optional[AnchorRate]:
    """Compute the indicator-specific multi-source anchor rate at end_year.

    Process
    -------
    1. Pick all sources whose `indicator_affinity` includes `indicator`.
    2. For each, get its smoothed YoY log-rate visible at end_year.
    3. Inverse-variance combine, with weights drawn from the
       calibration RMSE table when available, else from each source's
       in-sample residual SD.
    4. Compute SE on the combined rate accounting for cross-source
       correlation ρ (default 0.6 — these are different measurements
       of overlapping macro shocks, not independent).

    Calibration use
    ---------------
    `calibration` is `{indicator: {source_name: out_of_sample_rmse}}`
    in fractional pct-error units (e.g. 0.045 = 4.5%). When supplied
    *both* the inverse-variance weighting and the per-source SE on
    log-rate are taken from this table — the calibration RMSE is the
    out-of-sample error of "use this source's smoothed rate to project
    h years and score against ACS truth", which dominates the in-sample
    YoY volatility for admin series like FHFA HPI. We convert it to a
    per-year log-rate SE via:
        se_log_rate ≈ rmse / horizon
    using the back-test horizon (default 2y, configurable via
    `calibration_horizon`). The in-sample SD acts as a floor so that
    indicators with no calibration entry still get a sane SE.

    Returns None if no source has data visible at end_year (the caller
    must then fall back to ACS-only models).
    """
    if sources is None:
        sources = available_sources(indicator)
    if not sources:
        return None

    source_names: list[str] = []
    rates: list[AnnualRate] = []
    for src in sources:
        rate = src.smoothed_annual_rate(end_year=end_year)
        if rate is None:
            continue
        source_names.append(src.name)
        rates.append(rate)

    if not rates:
        return None

    per_source_rmse = (calibration or {}).get(indicator, {})

    # Replace per-source SE with calibration-derived SE when available.
    # Floor at the in-sample SD so we never drop *below* what the source's
    # own time-series suggests.
    horizon = max(int(calibration_horizon), 1)
    rates_calibrated: list[AnnualRate] = []
    for name, r in zip(source_names, rates):
        if name in per_source_rmse:
            cal_se = per_source_rmse[name] / horizon
            new_se = max(r.se_log_rate, cal_se)
            rates_calibrated.append(AnnualRate(year=r.year, log_rate=r.log_rate, se_log_rate=new_se))
        else:
            rates_calibrated.append(r)

    weights = _inverse_variance_weights(
        source_names, rates_calibrated,
        indicator_rmse=per_source_rmse if per_source_rmse else None,
    )

    point = sum(w * r.log_rate for w, r in zip(weights, rates_calibrated))

    # Variance of weighted sum with cross-source correlation rho.
    n = len(weights)
    var = 0.0
    for i in range(n):
        for j in range(n):
            corr = 1.0 if i == j else correlation_rho
            var += (
                weights[i] * weights[j]
                * rates_calibrated[i].se_log_rate * rates_calibrated[j].se_log_rate
                * corr
            )
    se = math.sqrt(var) if var > 0 else 0.0

    components = [
        (source_names[i], rates_calibrated[i].log_rate,
         rates_calibrated[i].se_log_rate, weights[i])
        for i in range(n)
    ]
    return AnchorRate(
        point_log_rate=point,
        se_log_rate=se,
        indicator=indicator,
        end_year=end_year,
        components=components,
    )


# -----------------------------------------------------------------------------
# Anchor-as-forecast
# -----------------------------------------------------------------------------

def anchor_as_forecast(
    latest: AcsObservation,
    target_year: int,
    anchor_rate: AnchorRate,
) -> ForecastPoint:
    """Project `latest` forward using the multi-source anchor's rate.

    Carries the anchor-rate SE *into* forecast SE — unlike the legacy
    `macro_anchor_projection` which treated the rate as truth. This is
    the correct uncertainty propagation for "the rate is itself an
    estimate, not a known constant".

    The implied anchored value at horizon h is:
        log(y_T) = log(y_anchor) + h · μ_rate
        Var(log y_T) ≈ Var(log y_anchor) + h² · Var(μ_rate)
    The first term is the ACS sample variance at the anchor; the
    second is the rate uncertainty propagated through h compoundings.
    Then we delta-method back to dollar space (σ_y ≈ σ_logy · y).
    """
    horizon = target_year - effective_year(latest)
    if horizon <= 0:
        sample_se = moe_to_se(latest.moe)
        if not math.isfinite(sample_se):
            sample_se = 0.0
        ci_lo, ci_hi = ci_from_se(latest.estimate, sample_se)
        return ForecastPoint(
            point=latest.estimate,
            se_total=sample_se,
            se_sample=sample_se,
            se_forecast=0.0,
            ci90_low=ci_lo,
            ci90_high=ci_hi,
            method="multi_anchor",
            target_year=target_year,
            geoid=latest.geoid,
            indicator=latest.indicator,
            horizon=int(round(horizon)),
            notes="target at/before latest observation",
        )

    # Cap the rate. ANNUAL_RATE_CAP is in linear-rate space; convert to
    # log space for the cap test. Annual rate cap of +10% → log(1.10) ≈ 0.0953.
    log_cap = math.log1p(ANNUAL_RATE_CAP)
    log_floor = math.log1p(-ANNUAL_RATE_CAP)
    rate = anchor_rate.point_log_rate
    notes = ""
    if rate > log_cap:
        notes = (
            f"anchor rate {math.expm1(rate) * 100:+.2f}%/yr "
            f"capped to {ANNUAL_RATE_CAP * 100:+.2f}%/yr"
        )
        rate = log_cap
    elif rate < log_floor:
        notes = (
            f"anchor rate {math.expm1(rate) * 100:+.2f}%/yr "
            f"capped to {-ANNUAL_RATE_CAP * 100:+.2f}%/yr"
        )
        rate = log_floor

    log_target = math.log(latest.estimate) + horizon * rate
    point = math.exp(log_target)

    # Sample SE at the anchor (relative-CV propagated to projection magnitude).
    sample_relative = (
        moe_to_se(latest.moe) / latest.estimate if latest.estimate > 0 else 0.0
    )
    if not math.isfinite(sample_relative):
        sample_relative = 0.0
    se_sample = sample_relative * point

    # Rate-uncertainty propagation through h compoundings: SE in log space
    # is h · SE(rate); times point in dollar space via delta method.
    se_rate_log = horizon * anchor_rate.se_log_rate * EMPIRICAL_SE_INFLATOR
    se_forecast = se_rate_log * point

    se_total = combine_se(se_sample, se_forecast)
    ci_lo, ci_hi = ci_from_se(point, se_total)

    component_str = "; ".join(
        f"{name}={w:.2f}@r={math.expm1(r) * 100:+.2f}%"
        for name, r, _se, w in anchor_rate.components
    )
    full_notes = f"sources[{component_str}]"
    if notes:
        full_notes += " | " + notes

    return ForecastPoint(
        point=point,
        se_total=se_total,
        se_sample=se_sample,
        se_forecast=se_forecast,
        ci90_low=ci_lo,
        ci90_high=ci_hi,
        method="multi_anchor",
        target_year=target_year,
        geoid=latest.geoid,
        indicator=latest.indicator,
        horizon=int(round(horizon)),
        notes=full_notes,
    )
