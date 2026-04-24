"""Unit tests for compute_cpi_ratio() and helpers in pipelines/grocery/src/price_adjuster.py.

These cover the projection edge cases introduced in the P0 refactor — particularly
the 'exact' vs 'projected' vs 'interpolated' vs 'unavailable' method disambiguation.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

# The grocery pipeline lives in its own package tree at pipelines/grocery/.
# Inserting its root lets us import from src.price_adjuster directly.
_GROCERY_ROOT = Path(__file__).resolve().parent.parent / "pipelines" / "grocery"
if str(_GROCERY_ROOT) not in sys.path:
    sys.path.insert(0, str(_GROCERY_ROOT))

from src.price_adjuster import compute_cpi_ratio, _project_forward  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_point(year: int, month: int, value: float) -> dict:
    """Build a BLS-shaped data point (year is int, matching cpi_fetcher output)."""
    return {"year": year, "period": f"M{month:02d}", "value": value}


def _cpi_data(series_id: str, points: list[dict]) -> dict:
    return {series_id: points}


SERIES = "CUURS49ASAF11"


# ---------------------------------------------------------------------------
# compute_cpi_ratio
# ---------------------------------------------------------------------------

class TestComputeCpiRatio:
    def test_exact_match(self):
        """When target == a bimonthly observation, method should be 'exact'."""
        pts = [
            _make_point(2024, 2, 300.0),
            _make_point(2024, 4, 303.0),
            _make_point(2024, 6, 306.0),
        ]
        cpi_data = _cpi_data(SERIES, pts)
        result = compute_cpi_ratio(cpi_data, SERIES,
                                   baseline_date=date(2024, 2, 1),
                                   target_date=date(2024, 6, 1))
        assert result["method"] == "exact"
        assert result["ratio"] == pytest.approx(306.0 / 300.0)
        assert not result["is_projected"]

    def test_interpolated_between_points(self):
        """Target between two observations → method 'interpolated'."""
        pts = [
            _make_point(2024, 2, 300.0),
            _make_point(2024, 4, 304.0),
        ]
        cpi_data = _cpi_data(SERIES, pts)
        result = compute_cpi_ratio(cpi_data, SERIES,
                                   baseline_date=date(2024, 2, 1),
                                   target_date=date(2024, 3, 1))
        assert result["method"] == "interpolated"
        # March is halfway between Feb and Apr → ratio ≈ 302 / 300
        assert result["ratio"] == pytest.approx(302.0 / 300.0, rel=1e-3)
        assert not result["is_projected"]

    def test_projected_beyond_latest(self):
        """Target past last observation → method 'projected', is_projected True."""
        pts = [
            _make_point(2024, 2, 300.0),
            _make_point(2024, 4, 306.0),
        ]
        cpi_data = _cpi_data(SERIES, pts)
        result = compute_cpi_ratio(cpi_data, SERIES,
                                   baseline_date=date(2024, 2, 1),
                                   target_date=date(2024, 6, 1))
        assert result["method"] == "projected"
        assert result["is_projected"] is True
        assert result["ratio"] > 1.0  # projection should exceed baseline

    def test_unavailable_empty_series(self):
        result = compute_cpi_ratio({}, SERIES,
                                   baseline_date=date(2024, 2, 1),
                                   target_date=date(2024, 4, 1))
        assert result["method"] == "unavailable"
        assert result["ratio"] == pytest.approx(1.0)
        assert not result["is_projected"]

    def test_same_baseline_and_target_ratio_is_one(self):
        """Identical baseline and target dates → ratio must be 1.0."""
        pts = [_make_point(2024, 4, 305.0)]
        cpi_data = _cpi_data(SERIES, pts)
        result = compute_cpi_ratio(cpi_data, SERIES,
                                   baseline_date=date(2024, 4, 1),
                                   target_date=date(2024, 4, 1))
        assert result["ratio"] == pytest.approx(1.0)

    def test_latest_observed_iso_format(self):
        pts = [
            _make_point(2024, 2, 300.0),
            _make_point(2024, 4, 305.0),
        ]
        cpi_data = _cpi_data(SERIES, pts)
        result = compute_cpi_ratio(cpi_data, SERIES,
                                   baseline_date=date(2024, 2, 1),
                                   target_date=date(2024, 4, 1))
        assert result["latest_observed"] == "2024-04"

    def test_missing_series_key_is_unavailable(self):
        result = compute_cpi_ratio({"other_series": []}, SERIES,
                                   baseline_date=date(2024, 2, 1),
                                   target_date=date(2024, 4, 1))
        assert result["method"] == "unavailable"


# ---------------------------------------------------------------------------
# _project_forward
# ---------------------------------------------------------------------------

class TestProjectForward:
    def test_flat_series_stays_flat(self):
        """Two identical points → growth rate 0 → projected value == latest."""
        pts = [
            _make_point(2024, 2, 300.0),
            _make_point(2024, 4, 300.0),
        ]
        result = _project_forward(pts, date(2024, 6, 1))
        assert result == pytest.approx(300.0)

    def test_growing_series_projects_higher(self):
        pts = [
            _make_point(2024, 2, 300.0),
            _make_point(2024, 4, 306.0),   # +1% per month over 2 months
        ]
        result = _project_forward(pts, date(2024, 6, 1))
        assert result > 306.0

    def test_monthly_rate_cap_applied(self):
        """A very noisy bimonthly spike should be capped at ±0.0189/month."""
        pts = [
            _make_point(2024, 2, 100.0),
            _make_point(2024, 4, 200.0),   # +100% in 2 months — far above cap
        ]
        # Cap: 0.0189/month × 2 months beyond = ~3.8% max increase from 200
        result = _project_forward(pts, date(2024, 6, 1))
        # Uncapped would be 200 * (1 + 0.414)^2 ≈ 283; capped should be ~207
        assert result < 210.0, f"cap not applied: {result}"

    def test_single_point_returns_that_value(self):
        """Only one observation → can't compute growth rate → return latest."""
        pts = [_make_point(2024, 4, 300.0)]
        result = _project_forward(pts, date(2024, 6, 1))
        assert result == pytest.approx(300.0)

    def test_empty_series_raises(self):
        with pytest.raises(ValueError, match="cannot project forward from empty series"):
            _project_forward([], date(2024, 6, 1))
