"""Tests for the hold-out calibration pipeline."""
from __future__ import annotations

import math
from collections import defaultdict

import pytest

from census_forecasting.src.calibration import (
    COVERAGE_LOWER_BOUND,
    COVERAGE_UPPER_BOUND,
    _normal_inv_cdf,
    run_holdout_calibration,
)
from census_forecasting.src.models import AcsObservation


def _build_synthetic_panel(
    indicators: list[str],
    geoids: list[str],
    years: range,
    growth_rate: float = 0.02,
) -> dict[tuple[str, str], list[AcsObservation]]:
    panel: dict[tuple[str, str], list[AcsObservation]] = defaultdict(list)
    for ind in indicators:
        for g in geoids:
            for i, y in enumerate(years):
                panel[(g, ind)].append(AcsObservation(
                    estimate=80000 * (1 + growth_rate) ** i,
                    moe=1500, year=y, vintage="1y",
                    geoid=g, indicator=ind,
                ))
    return panel


class TestRunHoldoutCalibration:
    def test_smoke_runs_clean_on_synthetic_panel(self):
        panel = _build_synthetic_panel(
            indicators=["B19013_001E", "B25064_001E"],
            geoids=["15001", "15003"],
            years=range(2010, 2025),
            growth_rate=0.025,
        )
        payload = run_holdout_calibration(
            series_by_key=panel,
            anchor_years=[2018, 2020, 2022],
            horizon=2,
        )
        assert payload["schema_version"] == 2
        assert "rmse_by_indicator_source" in payload
        assert "rmse_by_indicator_method" in payload
        assert "ci90_coverage_by_indicator_method" in payload

    def test_per_source_rmse_populated_for_admissible_sources(self):
        panel = _build_synthetic_panel(
            indicators=["B19013_001E"],
            geoids=["15003"],
            years=range(2010, 2025),
        )
        payload = run_holdout_calibration(panel, [2018, 2020, 2022], horizon=2)
        per_src = payload["rmse_by_indicator_source"].get("B19013_001E", {})
        # B19013 has CPI / PCE / QCEW affinity → at least 3 sources scored.
        assert len(per_src) >= 3
        for _name, rmse in per_src.items():
            assert math.isfinite(rmse)
            assert rmse >= 0

    def test_post_override_coverage_no_worse_than_baseline(self):
        """Override pass must not make coverage *worse*. On a noisy
        synthetic panel, baseline coverage may already be in band; we
        require the override to either keep it in band or — for
        in-band cells — leave it unchanged."""
        import random
        rng = random.Random(0xC0FFEE)
        panel: dict = defaultdict(list)
        # Add multiplicative log-normal noise to make the series realistic.
        for ind in ["B19013_001E"]:
            for g in ["15001", "15003", "15007", "15009"]:
                for i, y in enumerate(range(2010, 2025)):
                    base = 80000 * (1.025 ** i)
                    noise = math.exp(rng.gauss(0, 0.04))
                    panel[(g, ind)].append(AcsObservation(
                        estimate=base * noise, moe=2500,
                        year=y, vintage="1y", geoid=g, indicator=ind,
                    ))
        payload = run_holdout_calibration(panel, [2015, 2017, 2019, 2021], horizon=2)
        cov_pre = payload["ci90_coverage_by_indicator_method"]
        cov_post = payload.get("ci90_coverage_post_override", {})
        for ind, by_m in cov_post.items():
            for m, cov in by_m.items():
                pre = cov_pre.get(ind, {}).get(m, math.nan)
                # Either in band, OR not worse than the un-overridden coverage.
                in_band = COVERAGE_LOWER_BOUND <= cov <= COVERAGE_UPPER_BOUND
                if not in_band:
                    # Distance from band must be no greater than baseline.
                    pre_dist = max(0, COVERAGE_LOWER_BOUND - pre, pre - COVERAGE_UPPER_BOUND)
                    post_dist = max(0, COVERAGE_LOWER_BOUND - cov, cov - COVERAGE_UPPER_BOUND)
                    assert post_dist <= pre_dist + 1e-9, \
                        f"{ind}/{m}: post-override cov {cov:.3f} farther from band than pre {pre:.3f}"

    def test_no_anchors_no_panel_returns_empty_tables(self):
        payload = run_holdout_calibration({}, [2018], horizon=2)
        assert payload["rmse_by_indicator_source"] == {}
        assert payload["rmse_by_indicator_method"] == {}


class TestNormalInverseCDF:
    def test_known_quantiles(self):
        assert _normal_inv_cdf(0.5) == pytest.approx(0.0, abs=0.001)
        assert _normal_inv_cdf(0.84) == pytest.approx(0.9945, abs=0.01)
        assert _normal_inv_cdf(0.975) == pytest.approx(1.96, abs=0.01)
        # Hawaii-specific: z(0.95) ≈ 1.645, the ACS MOE convention.
        assert _normal_inv_cdf(0.95) == pytest.approx(1.645, abs=0.01)

    def test_symmetry(self):
        for p in (0.1, 0.2, 0.3, 0.4):
            assert _normal_inv_cdf(p) == pytest.approx(-_normal_inv_cdf(1 - p), abs=0.01)
