"""Stress tests for the projection pipeline.

Each test injects a known pathology — synthetic outliers, missing
vintages, extreme growth, degenerate inputs — and asserts the pipeline
either (a) produces a sane bounded output or (b) refuses cleanly with
None. Silent NaN propagation, CI half-widths exceeding 10× the point,
or negative SEs are all failures.
"""
from __future__ import annotations

import math

import pytest

from census_forecasting.src.anchors import (
    AnchorRate,
    anchor_as_forecast,
    combined_anchor_rate,
)
from census_forecasting.src.calibration import _normal_inv_cdf
from census_forecasting.src.ensemble import (
    project_ensemble,
    project_ensemble_multi,
)
from census_forecasting.src.models import AcsObservation
from census_forecasting.src.projection import (
    ANNUAL_RATE_CAP,
    fit_damped_trend,
    project_ar1_log_diff,
    project_damped_trend,
)
from census_forecasting.src.sources import available_sources


def _obs(year, est, moe=1000.0, vintage="1y", indicator="B19013_001E"):
    return AcsObservation(
        estimate=est, moe=moe, year=year, vintage=vintage,
        geoid="15003", indicator=indicator,
    )


# ---------------------------------------------------------------------------
# Synthetic outliers
# ---------------------------------------------------------------------------

class TestOutlierInjection:
    def test_late_spike_does_not_explode_projection(self):
        """A 2-sigma spike in the most recent year should not let the
        projection exceed the cap, even though the spike is the most
        recent signal and would dominate a naive single-pair init."""
        obs = [_obs(y, 100.0) for y in range(2014, 2024)]
        obs.append(_obs(2024, 200.0))  # +100% spike
        fp = project_damped_trend(obs, target_year=2026)
        rate = (fp.point / 200.0) ** (1.0 / fp.horizon) - 1.0
        assert rate <= ANNUAL_RATE_CAP + 1e-6

    def test_mid_series_outlier_dampened(self):
        """A single outlier in the middle of an otherwise smooth series
        should perturb the projection by < 50% of the outlier amplitude.
        Validates the damped trend's robustness."""
        clean = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(range(2014, 2025))]
        dirty = list(clean)
        # Replace year 2018 with a 50% outlier.
        dirty[4] = _obs(2018, clean[4].estimate * 1.5)
        fp_clean = project_damped_trend(clean, target_year=2026)
        fp_dirty = project_damped_trend(dirty, target_year=2026)
        diff_pct = abs(fp_dirty.point - fp_clean.point) / fp_clean.point
        assert diff_pct < 0.05  # well-damped: < 5% change

    def test_extreme_negative_outlier_clamped(self):
        """A −90% outlier in any single year shouldn't drive the projection
        below zero or trigger the cap multiple times."""
        obs = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(range(2014, 2024))]
        obs.append(_obs(2024, 10.0))  # −90% drop
        fp = project_damped_trend(obs, target_year=2026)
        assert fp.point > 0
        rate = (fp.point / 10.0) ** (1.0 / fp.horizon) - 1.0
        assert rate >= -ANNUAL_RATE_CAP - 1e-6


# ---------------------------------------------------------------------------
# Missing vintages / gaps
# ---------------------------------------------------------------------------

class TestMissingVintages:
    def test_handles_2020_gap_implicit(self):
        years = [2015, 2016, 2017, 2018, 2019, 2021, 2022, 2023, 2024]
        obs = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(years)]
        fp = project_damped_trend(obs, target_year=2026)
        assert fp is not None
        assert fp.point > 0

    def test_multiple_gap_years_still_projects(self):
        years = [2010, 2014, 2018, 2022]  # 4-year gaps
        obs = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(years)]
        fp = project_damped_trend(obs, target_year=2024)
        assert fp is not None

    def test_only_one_observation_returns_none(self):
        obs = [_obs(2024, 100)]
        assert project_damped_trend(obs, target_year=2026) is None
        assert project_ar1_log_diff(obs, target_year=2026) is None
        assert project_ensemble(obs, target_year=2026) is None

    def test_ensemble_falls_back_to_anchor_when_trend_fails(self):
        """One observation alone — trend ensemble fails, but multi-anchor
        can still project off the latest value at the macro rate."""
        obs = [_obs(2024, 100, moe=10)]
        fp = project_ensemble_multi(
            obs, target_year=2026,
            calibration=None,  # no calibration → equal-weight anchor combo
        )
        assert fp is not None
        # Sanity: anchor rate is small (a few %/yr); projection within ±20% of latest.
        assert 80 < fp.point < 130


# ---------------------------------------------------------------------------
# Extreme growth / degenerate inputs
# ---------------------------------------------------------------------------

class TestExtremes:
    def test_growth_at_30pct_caps_at_10pct(self):
        obs = [_obs(y, 100 * (1.30 ** i)) for i, y in enumerate(range(2018, 2025))]
        fp = project_damped_trend(obs, target_year=2027)
        rate = (fp.point / obs[-1].estimate) ** (1.0 / fp.horizon) - 1.0
        assert rate <= ANNUAL_RATE_CAP + 1e-6

    def test_decline_at_30pct_floors_at_minus_10pct(self):
        obs = [_obs(y, 100 * (0.70 ** i)) for i, y in enumerate(range(2018, 2025))]
        fp = project_damped_trend(obs, target_year=2027)
        rate = (fp.point / obs[-1].estimate) ** (1.0 / fp.horizon) - 1.0
        assert rate >= -ANNUAL_RATE_CAP - 1e-6

    def test_zero_estimate_skipped_not_logged(self):
        obs = [_obs(2018, 100), _obs(2019, 0), _obs(2020, 110), _obs(2021, 120)]
        fp = project_damped_trend(obs, target_year=2024)
        assert fp is not None
        assert math.isfinite(fp.point)

    def test_constant_zero_moe_no_division_by_zero(self):
        obs = [_obs(y, 100 * (1.02 ** i), moe=0.0) for i, y in enumerate(range(2014, 2025))]
        fp = project_damped_trend(obs, target_year=2026)
        assert fp is not None
        assert math.isfinite(fp.se_total)
        assert fp.ci90_low <= fp.point <= fp.ci90_high

    def test_explosive_ar1_variance_remains_finite(self):
        """ρ near 1 makes h-step variance grow polynomially. The cap on
        the *point* keeps growth sane; the *variance* should remain
        finite — never NaN, never negative."""
        obs = []
        log_y = math.log(100)
        diff = 0.05
        for y in range(2010, 2025):
            obs.append(_obs(y, math.exp(log_y)))
            log_y += diff
        fp = project_ar1_log_diff(obs, target_year=2030)
        assert fp is not None
        assert math.isfinite(fp.se_total)
        assert fp.se_total >= 0


# ---------------------------------------------------------------------------
# Anchor source / multi-source robustness
# ---------------------------------------------------------------------------

class TestAnchorSourcesStress:
    def test_no_source_for_unknown_indicator_returns_none(self):
        rate = combined_anchor_rate(indicator="ZZZ_FAKE_001E", end_year=2024)
        assert rate is None

    def test_publication_lag_excludes_future_data(self):
        """HUD FMR has a 2-year publication lag; a back-test at anchor
        2020 must not see the FY2024 FMR."""
        from census_forecasting.src.sources import load_source
        src = load_source("hud_fmr_honolulu")
        visible = src.load_series(end_year=2020)
        years_visible = [y for y, _ in visible]
        # Anchor 2020 + lag 2 → only years up to 2018 are visible.
        assert max(years_visible) <= 2018

    def test_anchor_se_floor_prevents_zero_se(self):
        """A perfectly smooth (synthetic) series with zero residual SD
        should still get the source's `rate_se_floor`, not zero."""
        from census_forecasting.src.sources import load_source
        src = load_source("cpi_honolulu_allitems")
        rate = src.smoothed_annual_rate(end_year=2024)
        assert rate is not None
        assert rate.se_log_rate >= src.rate_se_floor

    def test_multi_anchor_rate_within_cap(self):
        """The combined anchor rate should always fall inside the cap."""
        rate = combined_anchor_rate(indicator="B19013_001E", end_year=2024)
        assert rate is not None
        annual_rate = math.expm1(rate.point_log_rate)
        assert -ANNUAL_RATE_CAP <= annual_rate <= ANNUAL_RATE_CAP


# ---------------------------------------------------------------------------
# Calibration / SE override behavior
# ---------------------------------------------------------------------------

class TestCalibrationOverride:
    def test_se_override_floors_at_sample_se(self):
        """An override that would shrink CI below the irreducible sample
        SE should clamp at sample SE, not produce sub-sample CIs."""
        from census_forecasting.src.ensemble import _apply_se_override
        from census_forecasting.src.models import ForecastPoint
        # Build a fake forecast where se_sample dominates se_forecast.
        fp = ForecastPoint(
            point=100.0, se_total=math.sqrt(5 ** 2 + 1 ** 2),
            se_sample=5.0, se_forecast=1.0,
            ci90_low=100 - 1.645 * math.sqrt(26),
            ci90_high=100 + 1.645 * math.sqrt(26),
            method="m", target_year=2026, geoid="15003",
            indicator="B19013_001E", horizon=2,
        )
        # Aggressive shrink override (would otherwise drop SE below 5).
        cal = {
            "se_inflator_override_by_indicator_method": {
                "B19013_001E": {"m": 0.1}  # very small new inflator
            }
        }
        new_fp = _apply_se_override(fp, "B19013_001E", "m", cal)
        # Total SE can never drop below the sample SE component.
        assert new_fp.se_total >= fp.se_sample - 1e-9

    def test_invalid_override_returns_unchanged(self):
        from census_forecasting.src.ensemble import _apply_se_override
        from census_forecasting.src.models import ForecastPoint
        fp = ForecastPoint(
            point=100, se_total=10, se_sample=2, se_forecast=9.8,
            ci90_low=80, ci90_high=120,
            method="m", target_year=2026, geoid="15003",
            indicator="B19013_001E", horizon=2,
        )
        cal_nan = {
            "se_inflator_override_by_indicator_method": {
                "B19013_001E": {"m": float("nan")}
            }
        }
        out = _apply_se_override(fp, "B19013_001E", "m", cal_nan)
        assert out.se_total == fp.se_total

    def test_normal_inv_cdf_against_known_quantiles(self):
        """Sanity-check the inverse-normal helper used to convert
        observed coverage to a Gaussian z-quantile."""
        # z(0.975) ≈ 1.960; z(0.95) ≈ 1.645; z(0.5) = 0
        assert _normal_inv_cdf(0.975) == pytest.approx(1.96, abs=0.01)
        assert _normal_inv_cdf(0.95) == pytest.approx(1.645, abs=0.01)
        assert _normal_inv_cdf(0.5) == pytest.approx(0.0, abs=0.001)

    def test_normal_inv_cdf_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            _normal_inv_cdf(0.0)
        with pytest.raises(ValueError):
            _normal_inv_cdf(1.0)
        with pytest.raises(ValueError):
            _normal_inv_cdf(1.5)


# ---------------------------------------------------------------------------
# Anchor-as-forecast variance propagation
# ---------------------------------------------------------------------------

class TestAnchorAsForecast:
    def test_zero_se_anchor_collapses_to_sample_se(self):
        """When the anchor rate has zero SE (perfect external signal),
        the projection's forecast SE should reduce to the propagated
        sample SE — no additional uncertainty is added."""
        latest = _obs(2024, 100, moe=10)
        rate = AnchorRate(
            point_log_rate=math.log(1.04),
            se_log_rate=0.0,
            indicator="B19013_001E",
            end_year=2024,
            components=[("dummy", math.log(1.04), 0.0, 1.0)],
        )
        fp = anchor_as_forecast(latest, target_year=2026, anchor_rate=rate)
        # se_forecast = h * se_log_rate * EMPIRICAL_SE_INFLATOR * point
        # With se_log_rate=0, se_forecast=0 → se_total ≈ se_sample
        assert fp.se_forecast == 0.0
        assert fp.se_total == pytest.approx(fp.se_sample, rel=1e-6)

    def test_horizon_zero_collapses_to_passthrough(self):
        latest = _obs(2024, 100, moe=10)
        rate = AnchorRate(
            point_log_rate=0.05, se_log_rate=0.01,
            indicator="B19013_001E", end_year=2024, components=[],
        )
        fp = anchor_as_forecast(latest, target_year=2024, anchor_rate=rate)
        assert fp.point == 100
        assert fp.horizon == 0

    def test_anchor_rate_outside_cap_is_clamped(self):
        latest = _obs(2024, 100, moe=0)
        rate = AnchorRate(
            point_log_rate=math.log(1.50),  # +50% rate, way above 10% cap
            se_log_rate=0.01,
            indicator="B19013_001E", end_year=2024, components=[],
        )
        fp = anchor_as_forecast(latest, target_year=2026, anchor_rate=rate)
        rate_implied = (fp.point / latest.estimate) ** (1.0 / fp.horizon) - 1.0
        assert rate_implied <= ANNUAL_RATE_CAP + 1e-6
        assert "capped" in fp.notes


# ---------------------------------------------------------------------------
# End-to-end multi-anchor robustness
# ---------------------------------------------------------------------------

class TestEnsembleMultiAnchorRobustness:
    def test_full_series_runs_clean(self):
        obs = [_obs(y, 90000 + 1500 * i, moe=2000) for i, y in enumerate(range(2010, 2025))]
        fp = project_ensemble_multi(obs, target_year=2026)
        assert fp is not None
        assert math.isfinite(fp.point)
        assert fp.ci90_low <= fp.point <= fp.ci90_high
        assert fp.se_total >= fp.se_sample

    def test_calibration_dict_threaded(self):
        cal = {
            "rmse_by_indicator_source": {"B19013_001E": {"qcew_hawaii_wages": 0.04}},
            "rmse_by_indicator_method": {"B19013_001E": {
                "trend_ensemble": 0.07, "multi_anchor": 0.04,
            }},
            "se_inflator_override_by_indicator_method": {},
        }
        obs = [_obs(y, 90000 + 1500 * i, moe=2000) for i, y in enumerate(range(2010, 2025))]
        fp = project_ensemble_multi(obs, target_year=2026, calibration=cal)
        assert fp is not None

    def test_unknown_indicator_falls_back_to_trend(self):
        obs = [
            AcsObservation(
                estimate=100 * (1.02 ** i), moe=10,
                year=y, vintage="1y", geoid="15003", indicator="ZZZ_FAKE_001E",
            )
            for i, y in enumerate(range(2014, 2025))
        ]
        fp = project_ensemble_multi(obs, target_year=2026)
        assert fp is not None
        # No anchor sources available → should be the trend ensemble.
        assert "trend_ensemble" in fp.method or "trend" in fp.notes
