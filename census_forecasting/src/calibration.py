"""Hidden-data hold-out calibration of anchor weights and SE inflators.

Drives the data-driven elements of the production projection:

1. **Per-(indicator, source) RMSE** — drives inverse-variance anchor
   weights inside `anchors.combined_anchor_rate`.
2. **Per-(indicator, method) RMSE** — drives the macro/trend blend
   weight inside `ensemble._calibrated_macro_weight`.
3. **Per-indicator EMPIRICAL_SE_INFLATOR_OVERRIDE** — derived from
   coverage of the projection's 90% CI on hold-out folds. If coverage
   is below 85% or above 95%, scale the inflator to bring it into
   band. Documented in METHODOLOGY.md.

The calibration is *fully out-of-sample*: for each anchor year T we
re-derive every source rate using only data with publication_year ≤ T,
project forward h years to T+h, and score against the actual ACS 1-year
print at T+h. We then aggregate RMSE across folds.

The output JSON has this shape:

```
{
  "schema_version": 2,
  "run_date": "...",
  "anchor_years": [...],
  "horizon": 2,
  "rmse_by_indicator_source": {
    "B19013_001E": {
      "cpi_honolulu_allitems": 0.045,
      "qcew_hawaii_wages":      0.029,
      ...
    }, ...
  },
  "rmse_by_indicator_method": {
    "B19013_001E": {
      "trend_ensemble": 0.061,
      "multi_anchor":   0.041,
      "ensemble_multi_anchor": 0.038,
    }, ...
  },
  "ci90_coverage_by_indicator_method": { ... },
  "se_inflator_override_by_indicator_method": { ... }
}
```

The `project_acs_2026.py` entry point loads this file (if present) and
threads it through `project_ensemble_multi`.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional, Sequence

from .models import AcsObservation, ForecastPoint
from .projection import (
    EMPIRICAL_SE_INFLATOR,
    effective_year,
    project_ar1_log_diff,
    project_damped_trend,
)
from .ensemble import combine_forecasts
from .anchors import (
    AnchorRate,
    anchor_as_forecast,
    combined_anchor_rate,
)
from .sources import available_sources


# Coverage band: if 90% CI hits anything outside this, we adjust the
# per-method inflator. 85% and 95% are textbook tolerances around 90%.
COVERAGE_LOWER_BOUND = 0.85
COVERAGE_UPPER_BOUND = 0.95


@dataclass
class HoldOutFold:
    """One hold-out evaluation."""
    indicator: str
    geoid: str
    anchor_year: int
    target_year: int
    horizon: int
    method: str
    actual: float
    projected: float
    ci90_low: float
    ci90_high: float


def _truncate(
    series: Sequence[AcsObservation], anchor_year: int
) -> list[AcsObservation]:
    return [o for o in series if effective_year(o) <= anchor_year]


def _project_trend_only(
    train: Sequence[AcsObservation], target_year: int
) -> Optional[ForecastPoint]:
    """Run the trend-only ensemble (damped + ar1, no anchor)."""
    components: list[ForecastPoint] = []
    f_damped = project_damped_trend(train, target_year)
    if f_damped is not None:
        components.append(f_damped)
    f_ar1 = project_ar1_log_diff(train, target_year)
    if f_ar1 is not None:
        components.append(f_ar1)
    if not components:
        return None
    return combine_forecasts(components, target_year, method_label="trend_ensemble")


def _project_anchor_only(
    train: Sequence[AcsObservation],
    target_year: int,
    anchor_year: int,
    indicator: str,
    per_source_rmse: Optional[dict[str, dict[str, float]]] = None,
) -> Optional[ForecastPoint]:
    """Project from the latest training observation using only the multi-source anchor."""
    if not train:
        return None
    rate = combined_anchor_rate(
        indicator=indicator,
        end_year=anchor_year,
        calibration=per_source_rmse,
    )
    if rate is None:
        return None
    return anchor_as_forecast(
        latest=train[-1],
        target_year=target_year,
        anchor_rate=rate,
    )


def _per_source_anchor_forecast(
    train: Sequence[AcsObservation],
    target_year: int,
    anchor_year: int,
    indicator: str,
    source_name: str,
) -> Optional[ForecastPoint]:
    """Project at a *single* source's smoothed rate (for per-source RMSE calibration)."""
    if not train:
        return None
    sources = [s for s in available_sources(indicator) if s.name == source_name]
    if not sources:
        return None
    src = sources[0]
    rate = src.smoothed_annual_rate(end_year=anchor_year)
    if rate is None:
        return None
    # Wrap the single rate in an AnchorRate-equivalent for `anchor_as_forecast`.
    single = AnchorRate(
        point_log_rate=rate.log_rate,
        se_log_rate=rate.se_log_rate,
        indicator=indicator,
        end_year=anchor_year,
        components=[(src.name, rate.log_rate, rate.se_log_rate, 1.0)],
    )
    return anchor_as_forecast(
        latest=train[-1],
        target_year=target_year,
        anchor_rate=single,
    )


def run_holdout_calibration(
    series_by_key: dict[tuple[str, str], Sequence[AcsObservation]],
    anchor_years: Sequence[int],
    horizon: int = 2,
) -> dict:
    """Run hold-out calibration across all (geoid, indicator) × anchor years.

    Two passes:
    1. Per-source RMSE pass — each source projects alone and we score
       its forecasts against ACS truth. Results feed inverse-variance
       weights for the anchor combiner.
    2. Per-method RMSE pass — using the per-source RMSE from pass 1,
       run the anchor combiner, the trend ensemble, and the joint
       ensemble; score each.

    Both passes use the same anchor years and horizon.
    """
    folds_pass1: list[HoldOutFold] = []
    folds_pass2: list[HoldOutFold] = []

    # Group indicators we'll iterate over.
    indicators_seen: set[str] = set()
    for (_g, ind) in series_by_key:
        indicators_seen.add(ind)

    # ---- Pass 1: per-source RMSE ----
    for (geoid, indicator), full in series_by_key.items():
        full_sorted = sorted(full, key=lambda o: (effective_year(o), o.vintage))
        for anchor in anchor_years:
            target_year = anchor + horizon
            actual_obs = next(
                (o for o in full_sorted if effective_year(o) == target_year and o.vintage == "1y"),
                None,
            )
            if actual_obs is None or actual_obs.estimate <= 0:
                continue
            train = _truncate(full_sorted, anchor)
            if not train:
                continue
            for src in available_sources(indicator):
                fp = _per_source_anchor_forecast(
                    train, target_year, anchor, indicator, src.name
                )
                if fp is None:
                    continue
                folds_pass1.append(HoldOutFold(
                    indicator=indicator, geoid=geoid,
                    anchor_year=anchor, target_year=target_year, horizon=horizon,
                    method=f"source:{src.name}",
                    actual=actual_obs.estimate, projected=fp.point,
                    ci90_low=fp.ci90_low, ci90_high=fp.ci90_high,
                ))

    # Aggregate pass 1 to RMSE per (indicator, source).
    rmse_by_indicator_source: dict[str, dict[str, float]] = {}
    for f in folds_pass1:
        if f.actual <= 0:
            continue
        ind = f.indicator
        src = f.method.split(":", 1)[1]
        rmse_by_indicator_source.setdefault(ind, {}).setdefault(src, [])  # type: ignore[arg-type]
        rmse_by_indicator_source[ind][src].append(  # type: ignore[union-attr]
            ((f.projected - f.actual) / f.actual) ** 2
        )
    # Reduce to RMSE.
    for ind in list(rmse_by_indicator_source.keys()):
        for src, sq_errs in list(rmse_by_indicator_source[ind].items()):  # type: ignore[union-attr]
            if not sq_errs:
                del rmse_by_indicator_source[ind][src]
                continue
            rmse_by_indicator_source[ind][src] = math.sqrt(
                sum(sq_errs) / len(sq_errs)
            )

    # ---- Pass 2: per-method RMSE using calibration from pass 1 ----
    rmse_by_indicator_method: dict[str, dict[str, list[float]]] = {}
    coverage_by_indicator_method: dict[str, dict[str, list[int]]] = {}

    for (geoid, indicator), full in series_by_key.items():
        full_sorted = sorted(full, key=lambda o: (effective_year(o), o.vintage))
        for anchor in anchor_years:
            target_year = anchor + horizon
            actual_obs = next(
                (o for o in full_sorted if effective_year(o) == target_year and o.vintage == "1y"),
                None,
            )
            if actual_obs is None or actual_obs.estimate <= 0:
                continue
            train = _truncate(full_sorted, anchor)
            if not train:
                continue

            method_runs: dict[str, ForecastPoint] = {}
            tr = _project_trend_only(train, target_year)
            if tr is not None:
                method_runs["trend_ensemble"] = tr
            an = _project_anchor_only(
                train, target_year, anchor, indicator,
                per_source_rmse=rmse_by_indicator_source,
            )
            if an is not None:
                method_runs["multi_anchor"] = an

            for name, fp in method_runs.items():
                bucket_r = rmse_by_indicator_method.setdefault(indicator, {}).setdefault(name, [])
                bucket_r.append(((fp.point - actual_obs.estimate) / actual_obs.estimate) ** 2)
                bucket_c = coverage_by_indicator_method.setdefault(indicator, {}).setdefault(name, [])
                bucket_c.append(1 if fp.ci90_low <= actual_obs.estimate <= fp.ci90_high else 0)

                folds_pass2.append(HoldOutFold(
                    indicator=indicator, geoid=geoid,
                    anchor_year=anchor, target_year=target_year, horizon=horizon,
                    method=name,
                    actual=actual_obs.estimate, projected=fp.point,
                    ci90_low=fp.ci90_low, ci90_high=fp.ci90_high,
                ))

    rmse_per_method: dict[str, dict[str, float]] = {}
    coverage_per_method: dict[str, dict[str, float]] = {}
    for ind, by_m in rmse_by_indicator_method.items():
        rmse_per_method[ind] = {
            m: math.sqrt(sum(v) / len(v)) if v else math.nan
            for m, v in by_m.items()
        }
    for ind, by_m in coverage_by_indicator_method.items():
        coverage_per_method[ind] = {
            m: sum(v) / len(v) if v else math.nan
            for m, v in by_m.items()
        }

    # SE inflator override per (indicator, method): bring coverage into [85%, 95%]
    # by scaling the implied SE such that the corresponding Gaussian z-quantile
    # would have hit ~90% on this fold population.
    #
    # Closed form for one pass:
    #     observed_z = z(coverage) where coverage = P(|Z| ≤ observed_z)
    #     factor = z(0.90) / observed_z = 1.645 / observed_z
    # For coverage already in band, factor ≈ 1.0 and we leave the global
    # EMPIRICAL_SE_INFLATOR alone.
    #
    # Why this is iterative + conservative
    # ------------------------------------
    # Coverage is a *discrete* fraction (k/n folds). For n=24, the
    # smallest non-zero shift is 1/24 ≈ 4.2%. A single closed-form
    # rescaling may overshoot — e.g. a cell at 95.8% (23/24) gets a
    # narrowing factor ~0.81, which can push it to 83.3% (20/24)
    # instead of landing at 21-22/24. We therefore search across
    # candidate overrides via repeated bisection between an "over-cover"
    # bound (factor that gave coverage ≥ 95%) and an "under-cover"
    # bound (factor that gave coverage < 85%), preferring the
    # over-cover bound on tie — better to be slightly conservative
    # than to under-state uncertainty. If no candidate ever lands
    # inside [85%, 95%], we keep the smallest factor that achieved
    # ≥ 85% coverage (or the original 1.30 if that already qualified).
    se_override: dict[str, dict[str, float]] = {}
    max_iter = 6
    history: dict[tuple[str, str], list[tuple[float, float]]] = {}  # (override, cov)
    coverage_current = {
        ind: {m: c for m, c in by_m.items()}
        for ind, by_m in coverage_per_method.items()
    }
    # Seed history with the un-overridden case.
    for ind, by_m in coverage_current.items():
        for m, cov in by_m.items():
            if not math.isfinite(cov):
                continue
            history.setdefault((ind, m), []).append((EMPIRICAL_SE_INFLATOR, cov))

    for _ in range(max_iter):
        any_changed = False
        next_overrides: dict[tuple[str, str], float] = {}
        for ind, by_m in coverage_current.items():
            for m, cov in by_m.items():
                if not math.isfinite(cov):
                    continue
                if COVERAGE_LOWER_BOUND <= cov <= COVERAGE_UPPER_BOUND:
                    continue
                hist = history[(ind, m)]
                # Two-sided bound: smallest override that gave cov ≥ lower,
                # largest override that gave cov ≤ upper.
                over = [(ov, c) for ov, c in hist if c > COVERAGE_UPPER_BOUND]
                under = [(ov, c) for ov, c in hist if c < COVERAGE_LOWER_BOUND]
                if over and under:
                    # Bisect between the over (largest under-cover override)
                    # and under (smallest over-cover override).
                    smallest_over = min(over, key=lambda x: x[0])[0]
                    largest_under = max(under, key=lambda x: x[0])[0]
                    new_override = round((smallest_over + largest_under) / 2.0, 4)
                else:
                    cov_clipped = max(0.50, min(0.999, cov))
                    observed_z = _normal_inv_cdf(0.5 + cov_clipped / 2.0)
                    target_z = 1.645
                    factor = target_z / max(observed_z, 1e-3)
                    prior = se_override.get(ind, {}).get(m, EMPIRICAL_SE_INFLATOR)
                    new_override = round(prior * factor, 4)
                if abs(new_override - se_override.get(ind, {}).get(m, EMPIRICAL_SE_INFLATOR)) < 1e-3:
                    continue
                next_overrides[(ind, m)] = new_override
                any_changed = True
        if not any_changed:
            break
        for (ind, m), v in next_overrides.items():
            se_override.setdefault(ind, {})[m] = v
        cal_for_verify = {
            "rmse_by_indicator_source": rmse_by_indicator_source,
            "se_inflator_override_by_indicator_method": se_override,
        }
        coverage_current = _verify_post_override_coverage(
            series_by_key, anchor_years, horizon, cal_for_verify,
        )
        for ind, by_m in coverage_current.items():
            for m, cov in by_m.items():
                if not math.isfinite(cov):
                    continue
                key = (ind, m)
                ov = se_override.get(ind, {}).get(m, EMPIRICAL_SE_INFLATOR)
                history.setdefault(key, []).append((ov, cov))

    # Pick the best override per cell: prefer in-band; if multiple in-band,
    # pick the override with coverage closest to 0.90; if none in-band,
    # pick the override with maximum coverage (conservative — over-covers).
    final_override: dict[str, dict[str, float]] = {}
    for (ind, m), hist in history.items():
        in_band = [(ov, c) for ov, c in hist if COVERAGE_LOWER_BOUND <= c <= COVERAGE_UPPER_BOUND]
        if in_band:
            best = min(in_band, key=lambda x: abs(x[1] - 0.90))
        else:
            best = max(hist, key=lambda x: x[1])
        if abs(best[0] - EMPIRICAL_SE_INFLATOR) > 1e-4:
            final_override.setdefault(ind, {})[m] = best[0]
    se_override = final_override

    cal_for_verify = {
        "rmse_by_indicator_source": rmse_by_indicator_source,
        "se_inflator_override_by_indicator_method": se_override,
    }
    coverage_post = _verify_post_override_coverage(
        series_by_key, anchor_years, horizon, cal_for_verify,
    )

    return {
        "schema_version": 2,
        "run_date": date.today().isoformat(),
        "anchor_years": list(anchor_years),
        "horizon": horizon,
        "rmse_by_indicator_source": rmse_by_indicator_source,
        "rmse_by_indicator_method": rmse_per_method,
        "ci90_coverage_by_indicator_method": coverage_per_method,
        "ci90_coverage_post_override": coverage_post,
        "se_inflator_override_by_indicator_method": se_override,
        "folds_pass1": [_fold_to_dict(f) for f in folds_pass1],
        "folds_pass2": [_fold_to_dict(f) for f in folds_pass2],
    }


def _verify_post_override_coverage(
    series_by_key: dict[tuple[str, str], Sequence[AcsObservation]],
    anchor_years: Sequence[int],
    horizon: int,
    calibration: dict,
) -> dict[str, dict[str, float]]:
    """Re-run pass 2 with the calibrated SE overrides to confirm CI bands."""
    # Local import to avoid a circular dependency.
    from .ensemble import _apply_se_override

    coverage: dict[str, dict[str, list[int]]] = {}
    for (geoid, indicator), full in series_by_key.items():
        full_sorted = sorted(full, key=lambda o: (effective_year(o), o.vintage))
        for anchor in anchor_years:
            target_year = anchor + horizon
            actual_obs = next(
                (o for o in full_sorted if effective_year(o) == target_year and o.vintage == "1y"),
                None,
            )
            if actual_obs is None or actual_obs.estimate <= 0:
                continue
            train = _truncate(full_sorted, anchor)
            if not train:
                continue

            tr = _project_trend_only(train, target_year)
            if tr is not None:
                tr = _apply_se_override(tr, indicator, "trend_ensemble", calibration)
                coverage.setdefault(indicator, {}).setdefault("trend_ensemble", []).append(
                    1 if tr.ci90_low <= actual_obs.estimate <= tr.ci90_high else 0
                )
            an = _project_anchor_only(
                train, target_year, anchor, indicator,
                per_source_rmse=calibration.get("rmse_by_indicator_source"),
            )
            if an is not None:
                an = _apply_se_override(an, indicator, "multi_anchor", calibration)
                coverage.setdefault(indicator, {}).setdefault("multi_anchor", []).append(
                    1 if an.ci90_low <= actual_obs.estimate <= an.ci90_high else 0
                )

    out: dict[str, dict[str, float]] = {}
    for ind, by_m in coverage.items():
        out[ind] = {m: sum(v) / len(v) if v else math.nan for m, v in by_m.items()}
    return out


def _fold_to_dict(f: HoldOutFold) -> dict:
    return {
        "indicator": f.indicator,
        "geoid": f.geoid,
        "anchor_year": f.anchor_year,
        "target_year": f.target_year,
        "horizon": f.horizon,
        "method": f.method,
        "actual": f.actual,
        "projected": f.projected,
        "ci90_low": f.ci90_low,
        "ci90_high": f.ci90_high,
        "in_ci": int(f.ci90_low <= f.actual <= f.ci90_high),
        "abs_pct_err": abs((f.projected - f.actual) / f.actual) if f.actual > 0 else math.nan,
    }


# -----------------------------------------------------------------------------
# Inverse normal CDF (Beasley-Springer-Moro approximation, sufficient
# precision for coverage→z conversion at this scale).
# -----------------------------------------------------------------------------

def _normal_inv_cdf(p: float) -> float:
    """Approximate inverse standard-normal CDF (Acklam algorithm).

    Used here only to convert observed CI coverage into a "this is what
    z must have been" quantile so we can scale the empirical SE inflator
    to bring 90% coverage into target band. Precision <1e-4 in the
    body of the distribution, which is far more than coverage-band
    targeting requires.
    """
    if not (0.0 < p < 1.0):
        raise ValueError(f"p must be in (0,1), got {p}")
    a = [-3.969683028665376e+01,  2.209460984245205e+02,
         -2.759285104469687e+02,  1.383577518672690e+02,
         -3.066479806614716e+01,  2.506628277459239e+00]
    b = [-5.447609879822406e+01,  1.615858368580409e+02,
         -1.556989798598866e+02,  6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00]
    d = [ 7.784695709041462e-03,  3.224671290700398e-01,
          2.445134137142996e+00,  3.754408661907416e+00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
           ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def write_calibration(
    payload: dict, path: Path
) -> None:
    """Persist calibration JSON. Drops `folds_*` arrays from the on-disk
    summary file (those go into the back-test report); keeps the small
    RMSE / coverage / override tables that the projection loads at runtime.
    """
    summary = {
        k: v for k, v in payload.items()
        if k not in ("folds_pass1", "folds_pass2")
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
