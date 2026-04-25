"""Tests for the ensemble combiner + macro-anchor blending."""
import math

import pytest

from census_forecasting.src.ensemble import (
    combine_forecasts,
    macro_anchor_projection,
    project_ensemble,
)
from census_forecasting.src.models import AcsObservation, ForecastPoint
from census_forecasting.src.projection import ANNUAL_RATE_CAP


def _obs(year, est, moe=1000.0, vintage="1y"):
    return AcsObservation(
        estimate=est, moe=moe, year=year, vintage=vintage,
        geoid="15003", indicator="B19013_001E",
    )


def _fp(point, se, method="m1", target_year=2026, horizon=2):
    return ForecastPoint(
        point=point, se_total=se, se_sample=se / 2, se_forecast=se / 2,
        ci90_low=point - 1.645 * se, ci90_high=point + 1.645 * se,
        method=method, target_year=target_year, geoid="15003",
        indicator="B19013_001E", horizon=horizon,
    )


class TestCombineForecasts:
    def test_inverse_variance_weighting(self):
        # Two forecasts: (100, σ=10) and (110, σ=5). Inverse-variance
        # weights: 1/100, 1/25 → normalised 1/5, 4/5. Combined point =
        # 100·1/5 + 110·4/5 = 20 + 88 = 108.
        a = _fp(100, 10)
        b = _fp(110, 5, method="m2")
        c = combine_forecasts([a, b], target_year=2026)
        assert c.point == pytest.approx(108.0, rel=1e-6)

    def test_combined_se_with_correlation(self):
        # Two forecasts with equal SE=5 → inverse-variance weights w=0.5
        # each. With cross-correlation ρ=0.7 the closed-form variance is
        #   Var = 2·w²·σ²·(1 + ρ)
        #       = 2·0.25·25·1.7 = 21.25
        #   SE  = sqrt(21.25) ≈ 4.6098
        a = _fp(100, 5)
        b = _fp(110, 5, method="m2")
        c = combine_forecasts([a, b], target_year=2026)
        expected = math.sqrt(2 * 0.25 * 25 * (1 + 0.7))
        assert c.se_total == pytest.approx(expected, rel=1e-6)

    def test_combined_ci_includes_point(self):
        a = _fp(100, 10)
        b = _fp(120, 5, method="m2")
        c = combine_forecasts([a, b], target_year=2026)
        assert c.ci90_low <= c.point <= c.ci90_high

    def test_empty_returns_none(self):
        assert combine_forecasts([], target_year=2026) is None

    def test_mismatched_target_raises(self):
        a = _fp(100, 10, target_year=2025)
        b = _fp(110, 5, method="m2", target_year=2026)
        with pytest.raises(ValueError):
            combine_forecasts([a, b], target_year=2026)

    def test_mismatched_geoid_raises(self):
        a = _fp(100, 10)
        b = ForecastPoint(
            point=110, se_total=5, se_sample=2, se_forecast=2,
            ci90_low=100, ci90_high=120, method="m2",
            target_year=2026, geoid="15001", indicator="B19013_001E", horizon=2,
        )
        with pytest.raises(ValueError):
            combine_forecasts([a, b], target_year=2026)

    def test_zero_variance_falls_back_to_equal_weights(self):
        # Degenerate case: if every forecast has zero SE, inverse-variance
        # is undefined. Should not divide by zero.
        a = _fp(100, 0)
        b = _fp(110, 0, method="m2")
        c = combine_forecasts([a, b], target_year=2026)
        # Equal-weight average: (100 + 110) / 2 = 105.
        assert c.point == pytest.approx(105.0, rel=1e-9)


class TestMacroAnchor:
    def test_basic_compounding(self):
        latest = _obs(2024, 100, moe=0)
        fp = macro_anchor_projection(latest, target_year=2026, annual_growth_rate=0.05)
        # 100 · 1.05² = 110.25
        assert fp.point == pytest.approx(110.25, rel=1e-9)

    def test_negative_rate_compounds(self):
        latest = _obs(2024, 100, moe=0)
        fp = macro_anchor_projection(latest, target_year=2026, annual_growth_rate=-0.03)
        assert fp.point == pytest.approx(100 * (0.97 ** 2), rel=1e-9)

    def test_extreme_rate_capped(self):
        latest = _obs(2024, 100, moe=0)
        fp = macro_anchor_projection(latest, target_year=2026, annual_growth_rate=0.50)
        # Cap kicks in at +10%/yr.
        assert fp.point == pytest.approx(100 * (1 + ANNUAL_RATE_CAP) ** 2, rel=1e-6)
        assert "capped" in fp.notes

    def test_negative_extreme_rate_capped(self):
        latest = _obs(2024, 100, moe=0)
        fp = macro_anchor_projection(latest, target_year=2026, annual_growth_rate=-0.40)
        assert fp.point == pytest.approx(100 * (1 - ANNUAL_RATE_CAP) ** 2, rel=1e-6)
        assert "capped" in fp.notes

    def test_horizon_zero_passthrough(self):
        latest = _obs(2024, 100, moe=10)
        fp = macro_anchor_projection(latest, target_year=2024, annual_growth_rate=0.05)
        assert fp.point == 100
        assert fp.method == "macro_anchor"


class TestProjectEnsemble:
    def test_full_pipeline_growing_series(self):
        obs = [_obs(y, 100 * (1.025 ** i)) for i, y in enumerate(range(2010, 2025))]
        fp = project_ensemble(obs, target_year=2026)
        assert fp is not None
        assert fp.method == "ensemble"
        assert fp.point > obs[-1].estimate

    def test_returns_none_with_empty_input(self):
        assert project_ensemble([], target_year=2026) is None

    def test_returns_none_with_one_obs_no_macro(self):
        # AR(1) needs ≥4, damped trend needs ≥2 — one observation alone
        # gives nothing to ensemble, no macro to fall back to → None.
        assert project_ensemble([_obs(2024, 100)], target_year=2026) is None

    def test_macro_anchor_fallback_with_sparse_data(self):
        # One observation + macro anchor → macro_anchor result.
        fp = project_ensemble(
            [_obs(2024, 100, moe=0)],
            target_year=2026,
            macro_annual_rate=0.04,
            macro_weight=0.30,
        )
        assert fp is not None
        # 1.04² = 1.0816
        assert fp.point == pytest.approx(108.16, rel=1e-6)

    def test_macro_blend_weight_respected(self):
        obs = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(range(2010, 2025))]
        # No macro: pure trend
        fp_trend = project_ensemble(obs, target_year=2026)
        # With macro at 0% growth, weight 0.5 → projection pulled toward latest.
        fp_blend = project_ensemble(
            obs, target_year=2026, macro_annual_rate=0.0, macro_weight=0.5,
        )
        # Blend should sit between latest (100·1.02^14 ≈ 132.0) and trend forecast.
        latest = obs[-1].estimate
        assert latest < fp_blend.point < fp_trend.point

    def test_ensemble_ci_includes_point(self):
        obs = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(range(2010, 2025))]
        fp = project_ensemble(obs, target_year=2026)
        assert fp.ci90_low <= fp.point <= fp.ci90_high

    def test_ensemble_includes_method_weights_in_notes(self):
        obs = [_obs(y, 100 * (1.02 ** i)) for i, y in enumerate(range(2010, 2025))]
        fp = project_ensemble(obs, target_year=2026)
        assert "damped_log_trend" in fp.notes
        assert "ar1_log_diff" in fp.notes
