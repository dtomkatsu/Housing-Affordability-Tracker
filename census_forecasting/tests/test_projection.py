"""Tests for the projection models — damped trend, AR(1), edge cases.

These tests pin the math against hand-computable cases (constant series,
geometric series, monotonic linear) so any future refactor of the model
internals has to either reproduce these numbers or explicitly justify
the change.
"""
import math

import pytest

from census_forecasting.src.models import AcsObservation
from census_forecasting.src.projection import (
    ANNUAL_RATE_CAP,
    DampedTrendFit,
    EMPIRICAL_SE_INFLATOR,
    effective_year,
    fit_ar1_log_diff,
    fit_damped_trend,
    project_ar1_log_diff,
    project_damped_trend,
)


def _obs(year: int, est: float, moe: float = 1000.0, vintage: str = "1y"):
    return AcsObservation(
        estimate=est, moe=moe, year=year, vintage=vintage,
        geoid="15003", indicator="B19013_001E",
    )


# ---------------------------------------------------------------------------
# effective_year — vintage time-axis convention
# ---------------------------------------------------------------------------

class TestEffectiveYear:
    def test_one_year_passthrough(self):
        assert effective_year(_obs(2024, est=100)) == 2024.0

    def test_five_year_midpoint(self):
        # 2020-2024 5y vintage → midpoint 2022.
        o = _obs(2024, est=100, vintage="5y")
        assert effective_year(o) == 2022.0

    def test_unknown_vintage_raises(self):
        # AcsObservation rejects unknown vintages at construction so we
        # build a rough mock to test the projection.effective_year guard.
        class FakeObs:
            year = 2024
            vintage = "10y"
        with pytest.raises(ValueError):
            effective_year(FakeObs())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fit_damped_trend — model fitting
# ---------------------------------------------------------------------------

class TestDampedTrendFit:
    def test_constant_series_yields_zero_trend(self):
        # If every observation is the same, the fitted trend should
        # collapse to zero (after a few warmup steps). Pinning zero
        # trend on a flat series guards against algebra mistakes that
        # would otherwise drift on no signal.
        obs = [_obs(y, 100.0) for y in range(2010, 2020)]
        fit = fit_damped_trend(obs)
        assert fit is not None
        assert abs(fit.trend) < 1e-6
        # Forecasted log should equal current level for any horizon on
        # a flat series.
        assert fit.project_log(5) == pytest.approx(fit.level, abs=1e-9)

    def test_geometric_series_recovers_growth_rate(self):
        # log(y_t) = log(100) + 0.05·t  →  trend ≈ 0.05
        obs = [_obs(y, 100.0 * (1.05 ** i)) for i, y in enumerate(range(2010, 2024))]
        fit = fit_damped_trend(obs, phi=0.95, alpha=0.6, beta=0.4)
        assert fit is not None
        # Holt's damped never recovers exact slope (φ<1 damps it), so
        # check we're close (within a few %).
        assert 0.035 < fit.trend < 0.055

    def test_too_few_observations_returns_none(self):
        assert fit_damped_trend([_obs(2024, 100)]) is None
        assert fit_damped_trend([]) is None

    def test_negative_values_dropped(self):
        # log() requires positive estimates; negatives must be skipped.
        obs = [_obs(2020, 100), _obs(2021, -1), _obs(2022, 110), _obs(2023, 120)]
        fit = fit_damped_trend(obs)
        assert fit is not None
        # Only 3 valid points went in.
        assert fit.n_obs == 3

    def test_handles_year_gaps_for_2020_missing(self):
        # 1-year ACS skipped 2020 — the fitter must handle a gap of 2.
        obs = [_obs(y, 100 * (1.03 ** i)) for i, y in enumerate(
            [2017, 2018, 2019, 2021, 2022, 2023]
        )]
        fit = fit_damped_trend(obs)
        assert fit is not None
        # Trend should be in the neighborhood of the true 3% growth.
        assert 0.015 < fit.trend < 0.045


class TestDampedTrendProjection:
    def test_horizon_zero_returns_passthrough(self):
        obs = [_obs(2020, 100), _obs(2024, 110)]
        fp = project_damped_trend(obs, target_year=2024)
        assert fp.method == "passthrough"
        assert fp.point == 110

    def test_target_in_past_returns_passthrough(self):
        obs = [_obs(2020, 100), _obs(2024, 110)]
        fp = project_damped_trend(obs, target_year=2023)
        assert fp.method == "passthrough"

    def test_growing_series_projects_above_latest(self):
        obs = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(range(2014, 2025))]
        fp = project_damped_trend(obs, target_year=2026)
        assert fp.point > obs[-1].estimate

    def test_declining_series_projects_below_latest(self):
        obs = [_obs(y, 200 * (0.98 ** i)) for i, y in enumerate(range(2014, 2025))]
        fp = project_damped_trend(obs, target_year=2026)
        assert fp.point < obs[-1].estimate

    def test_cap_applied_to_extreme_growth(self):
        # Build a series that grows 30%/yr — cap should clamp at +10%/yr.
        obs = [_obs(y, 100 * (1.30 ** i)) for i, y in enumerate(range(2018, 2025))]
        fp = project_damped_trend(obs, target_year=2027)
        # Implied annual growth from anchor → projection
        years = fp.horizon
        rate = (fp.point / obs[-1].estimate) ** (1.0 / years) - 1.0
        assert rate <= ANNUAL_RATE_CAP + 1e-6
        assert "capped" in fp.notes

    def test_cap_applied_to_extreme_decline(self):
        obs = [_obs(y, 100 * (0.70 ** i)) for i, y in enumerate(range(2018, 2025))]
        fp = project_damped_trend(obs, target_year=2027)
        years = fp.horizon
        rate = (fp.point / obs[-1].estimate) ** (1.0 / years) - 1.0
        assert rate >= -ANNUAL_RATE_CAP - 1e-6
        assert "capped" in fp.notes

    def test_ci_includes_point(self):
        obs = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(range(2014, 2025))]
        fp = project_damped_trend(obs, target_year=2026)
        assert fp.ci90_low <= fp.point <= fp.ci90_high

    def test_ci_widens_with_horizon(self):
        # Same series, two horizons — the longer horizon should have a
        # strictly wider CI under any honest variance model.
        obs = [_obs(y, 100 * (1.02 ** i), moe=10) for i, y in enumerate(range(2014, 2025))]
        fp1 = project_damped_trend(obs, target_year=2026)
        fp2 = project_damped_trend(obs, target_year=2030)
        assert (fp2.ci90_high - fp2.ci90_low) > (fp1.ci90_high - fp1.ci90_low)

    def test_se_components_combine_in_quadrature(self):
        obs = [_obs(y, 100 * (1.02 ** i), moe=50) for i, y in enumerate(range(2014, 2025))]
        fp = project_damped_trend(obs, target_year=2026)
        expected = math.sqrt(fp.se_sample ** 2 + fp.se_forecast ** 2)
        assert fp.se_total == pytest.approx(expected, rel=1e-6)


class TestDampedTrendVarianceFormula:
    """Pin Hyndman ETS(A,Ad,N) closed-form against a hand-computed value."""

    def test_h_equals_one_collapses_to_residual_std(self):
        # At h=1 the variance factor is 1, so SE_log = residual_std *
        # small_sample * EMPIRICAL_SE_INFLATOR (no h-step propagation).
        fit = DampedTrendFit(
            level=math.log(100), trend=0.02, alpha=0.6, beta=0.2, phi=0.85,
            residual_std=0.05, last_year=2024, n_obs=10,
        )
        # n=10 → small-sample correction = 10/8 = 1.25 → factor sqrt(1.25)
        expected = 0.05 * math.sqrt(1.25) * EMPIRICAL_SE_INFLATOR
        assert fit.forecast_se_log(1) == pytest.approx(expected, rel=1e-9)

    def test_h_two_uses_hyndman_coefficient(self):
        # c_1 = α(1 + β·φ·(1−φ)/(1−φ)) = α(1 + β·φ)
        # var_factor = 1 + c_1²
        fit = DampedTrendFit(
            level=0, trend=0, alpha=0.6, beta=0.2, phi=0.85,
            residual_std=0.1, last_year=0, n_obs=10,
        )
        c1 = 0.6 * (1 + 0.2 * 0.85)
        var_factor = 1 + c1 ** 2
        small = 10 / 8
        expected = math.sqrt(0.01 * var_factor * small) * EMPIRICAL_SE_INFLATOR
        assert fit.forecast_se_log(2) == pytest.approx(expected, rel=1e-9)

    def test_undamped_limit_phi_one(self):
        # When φ=1, c_j = α(1 + β·j). Test j=1 → c_1 = α(1+β).
        fit = DampedTrendFit(
            level=0, trend=0, alpha=0.5, beta=0.3, phi=1.0,
            residual_std=0.1, last_year=0, n_obs=10,
        )
        c1 = 0.5 * (1 + 0.3)
        var_factor = 1 + c1 ** 2
        expected = math.sqrt(0.01 * var_factor * (10 / 8)) * EMPIRICAL_SE_INFLATOR
        assert fit.forecast_se_log(2) == pytest.approx(expected, rel=1e-9)

    def test_horizon_zero_zero_se(self):
        fit = DampedTrendFit(
            level=0, trend=0, alpha=0.6, beta=0.2, phi=0.85,
            residual_std=0.1, last_year=0, n_obs=10,
        )
        assert fit.forecast_se_log(0) == 0.0


# ---------------------------------------------------------------------------
# AR(1) on log-diffs
# ---------------------------------------------------------------------------

class TestAR1LogDiff:
    def test_too_few_observations_returns_none(self):
        # Need at least 4 observations to fit AR(1) + estimate residuals.
        obs = [_obs(y, 100) for y in range(2020, 2023)]
        assert fit_ar1_log_diff(obs) is None

    def test_constant_series_zero_intercept_zero_rho(self):
        obs = [_obs(y, 100) for y in range(2014, 2025)]
        fit = fit_ar1_log_diff(obs)
        assert fit is not None
        assert abs(fit.intercept) < 1e-9
        # rho is undefined when all log-diffs are zero (denom=0); fitter
        # falls back to rho=0, which is correct conservative behavior.
        assert fit.rho == pytest.approx(0.0, abs=1e-9)

    def test_geometric_series_constant_diff(self):
        # All log-diffs ≈ log(1.03), so AR(1) intercept ≈ log(1.03), rho ≈ 0.
        obs = [_obs(y, 100 * (1.03 ** i)) for i, y in enumerate(range(2010, 2024))]
        fit = fit_ar1_log_diff(obs)
        assert fit is not None
        assert fit.intercept == pytest.approx(math.log(1.03), abs=0.01)

    def test_rho_clamped_to_stationary(self):
        # Build a near-random-walk-on-diffs to push rho high.
        # The fitter clamps |rho| < 0.95 to keep multi-step forecasts sane.
        # Use a deterministic series where each diff is the previous diff
        # exactly: that yields rho=1 unconstrained; we expect 0.95 cap.
        obs = []
        log_y = math.log(100)
        diff = 0.01
        for i, y in enumerate(range(2010, 2024)):
            obs.append(_obs(y, math.exp(log_y)))
            log_y += diff
            diff += 0.001  # accelerating
        fit = fit_ar1_log_diff(obs)
        assert fit is not None
        assert abs(fit.rho) <= 0.95

    def test_projection_caps_extreme_implied_rate(self):
        # Series with explosive growth — the cap on cumulative log-diff
        # should clamp the projected growth.
        obs = [_obs(y, 100 * (1.40 ** i)) for i, y in enumerate(range(2015, 2025))]
        fp = project_ar1_log_diff(obs, target_year=2027)
        assert fp is not None
        rate = (fp.point / obs[-1].estimate) ** (1.0 / fp.horizon) - 1.0
        assert rate <= ANNUAL_RATE_CAP + 1e-6

    def test_projection_returns_none_for_short_series(self):
        obs = [_obs(y, 100) for y in range(2020, 2023)]
        assert project_ar1_log_diff(obs, target_year=2025) is None

    def test_forecast_se_includes_sample_se(self):
        # Anchor MOE >>> model residuals → projection SE dominated by sample.
        obs = [_obs(y, 100, moe=50) for y in range(2010, 2025)]
        fp = project_ar1_log_diff(obs, target_year=2026)
        assert fp.se_sample > 0
        assert fp.se_total >= fp.se_sample
