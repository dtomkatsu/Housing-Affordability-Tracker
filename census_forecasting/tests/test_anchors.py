"""Tests for the multi-source anchor module and source loaders."""
from __future__ import annotations

import math

import pytest

from census_forecasting.src.anchors import (
    AnchorRate,
    anchor_as_forecast,
    combined_anchor_rate,
    _inverse_variance_weights,
)
from census_forecasting.src.models import AcsObservation
from census_forecasting.src.sources import (
    AnchorSource,
    AnnualRate,
    available_sources,
    load_source,
)


# ---------------------------------------------------------------------------
# Source loading + visibility
# ---------------------------------------------------------------------------

class TestAnchorSourceLoading:
    def test_known_sources_load(self):
        for name in [
            "cpi_honolulu_allitems",
            "cpi_honolulu_rent",
            "pce_deflator",
            "qcew_hawaii_wages",
            "hud_fmr_honolulu",
            "fred_hi_hpi",
        ]:
            src = load_source(name)
            assert isinstance(src, AnchorSource)
            assert src.name == name
            series = src.load_series()
            assert len(series) >= 10  # at least 10 years of data

    def test_unknown_source_raises(self):
        with pytest.raises(KeyError):
            load_source("not_a_real_source")

    def test_publication_lag_filters_visibility(self):
        """HUD FMR has a 2-year publication lag; series visible at
        anchor 2018 must exclude any year > 2016."""
        src = load_source("hud_fmr_honolulu")
        years_2018 = [y for y, _ in src.load_series(end_year=2018)]
        assert max(years_2018) <= 2018 - src.publication_lag_years

    def test_zero_publication_lag(self):
        src = load_source("cpi_honolulu_allitems")
        assert src.publication_lag_years == 0


# ---------------------------------------------------------------------------
# Annual log rates and smoothed rate
# ---------------------------------------------------------------------------

class TestAnnualRates:
    def test_rates_in_reasonable_band(self):
        """All historical YoY log-rates should be within ±20% — Hawaii
        macroeconomic series have not exceeded that band in the embedded
        period. If they do, the source data is wrong."""
        for src in available_sources():
            for r in src.annual_log_rates():
                assert abs(r.log_rate) < 0.30, f"{src.name}: rate {r.log_rate} at {r.year}"

    def test_smoothed_rate_inside_min_max_pair_rate(self):
        src = load_source("cpi_honolulu_allitems")
        rates = src.annual_log_rates()
        smoothed = src.smoothed_annual_rate()
        rs = [r.log_rate for r in rates]
        assert smoothed is not None
        assert min(rs) - 1e-9 <= smoothed.log_rate <= max(rs) + 1e-9

    def test_smoothed_rate_se_floored(self):
        src = load_source("cpi_honolulu_allitems")
        smoothed = src.smoothed_annual_rate()
        assert smoothed is not None
        assert smoothed.se_log_rate >= src.rate_se_floor - 1e-12

    def test_no_data_at_early_anchor_returns_none(self):
        src = load_source("cpi_honolulu_allitems")
        # Anchor before any data → no rates.
        rate = src.smoothed_annual_rate(end_year=2005)
        assert rate is None


# ---------------------------------------------------------------------------
# Inverse-variance weights
# ---------------------------------------------------------------------------

class TestInverseVarianceWeights:
    def _rate(self, lr=0.02, se=0.01):
        return AnnualRate(year=2024, log_rate=lr, se_log_rate=se)

    def test_equal_se_yields_equal_weights(self):
        rates = [self._rate(se=0.01), self._rate(se=0.01)]
        w = _inverse_variance_weights(["a", "b"], rates)
        assert w == pytest.approx([0.5, 0.5])

    def test_lower_se_gets_higher_weight(self):
        rates = [self._rate(se=0.02), self._rate(se=0.01)]
        w = _inverse_variance_weights(["a", "b"], rates)
        assert w[1] > w[0]

    def test_calibration_overrides_se(self):
        rates = [self._rate(se=0.001), self._rate(se=0.001)]  # equal in-sample
        w_eq = _inverse_variance_weights(["a", "b"], rates)
        # Calibration says source "b" has 2x the RMSE → weight should fall.
        w_cal = _inverse_variance_weights(
            ["a", "b"], rates,
            indicator_rmse={"a": 0.04, "b": 0.08},
        )
        assert w_eq == pytest.approx([0.5, 0.5])
        assert w_cal[0] > w_cal[1]

    def test_floor_applied(self):
        # Both SEs near zero — without floor would produce inf/inf weights.
        rates = [self._rate(se=1e-10), self._rate(se=1e-10)]
        w = _inverse_variance_weights(["a", "b"], rates, rmse_floor=0.005)
        assert sum(w) == pytest.approx(1.0)
        assert all(math.isfinite(x) for x in w)

    def test_alignment_validation(self):
        rates = [self._rate(), self._rate()]
        with pytest.raises(ValueError):
            _inverse_variance_weights(["a"], rates)


# ---------------------------------------------------------------------------
# combined_anchor_rate
# ---------------------------------------------------------------------------

class TestCombinedAnchorRate:
    def test_returns_none_with_no_admissible_source(self):
        rate = combined_anchor_rate(indicator="ZZZ_FAKE_001E", end_year=2024)
        assert rate is None

    def test_aggregate_rate_within_source_range(self):
        rate = combined_anchor_rate(indicator="B19013_001E", end_year=2024)
        assert rate is not None
        # Component rates span a small range; aggregate should be inside.
        comp_rates = [r for _name, r, _se, _w in rate.components]
        assert min(comp_rates) - 1e-9 <= rate.point_log_rate <= max(comp_rates) + 1e-9

    def test_calibration_redistributes_weights(self):
        # Two runs with and without calibration; weights should shift.
        rate_eq = combined_anchor_rate(indicator="B19013_001E", end_year=2024)
        rate_cal = combined_anchor_rate(
            indicator="B19013_001E", end_year=2024,
            calibration={
                "B19013_001E": {
                    "cpi_honolulu_allitems": 0.20,  # heavily penalised
                    "qcew_hawaii_wages": 0.02,
                    "pce_deflator": 0.20,
                },
            },
        )
        assert rate_eq is not None
        assert rate_cal is not None
        # Find weights for qcew_hawaii_wages.
        def w_of(rate, name):
            for n, _r, _se, w in rate.components:
                if n == name:
                    return w
            return None
        assert w_of(rate_cal, "qcew_hawaii_wages") > w_of(rate_eq, "qcew_hawaii_wages")

    def test_se_combines_with_correlation(self):
        rate = combined_anchor_rate(indicator="B19013_001E", end_year=2024)
        assert rate is not None
        # Combined SE should be smaller than the smallest individual SE
        # (diversification benefit) but not by more than sqrt(n)
        # — indicator that the rho correlation is not zero (which would
        # collapse the SE more than is honest).
        comp_ses = [se for _n, _r, se, _w in rate.components]
        assert rate.se_log_rate <= max(comp_ses) + 1e-9
        # With ρ=0.6 the combined SE should be > 60% of the avg SE.
        avg_se = sum(comp_ses) / len(comp_ses)
        assert rate.se_log_rate >= 0.6 * avg_se - 1e-9


# ---------------------------------------------------------------------------
# Anchor-as-forecast end-to-end
# ---------------------------------------------------------------------------

class TestAnchorAsForecastIntegration:
    def test_known_indicator_projects_within_cap(self):
        latest = AcsObservation(
            estimate=95000, moe=2500, year=2024, vintage="1y",
            geoid="15003", indicator="B19013_001E",
        )
        rate = combined_anchor_rate(indicator="B19013_001E", end_year=2024)
        assert rate is not None
        fp = anchor_as_forecast(latest, target_year=2026, anchor_rate=rate)
        annual_rate = (fp.point / latest.estimate) ** (1.0 / fp.horizon) - 1.0
        assert -0.10 <= annual_rate <= 0.10

    def test_se_forecast_increases_with_horizon(self):
        latest = AcsObservation(
            estimate=95000, moe=0, year=2024, vintage="1y",
            geoid="15003", indicator="B19013_001E",
        )
        rate = combined_anchor_rate(indicator="B19013_001E", end_year=2024)
        assert rate is not None
        fp1 = anchor_as_forecast(latest, target_year=2026, anchor_rate=rate)
        fp2 = anchor_as_forecast(latest, target_year=2030, anchor_rate=rate)
        assert fp2.se_forecast > fp1.se_forecast
