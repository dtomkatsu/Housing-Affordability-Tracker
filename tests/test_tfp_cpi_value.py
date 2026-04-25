"""Unit tests for tfp-updater._cpi_value_for() and project_tfp_forward().

These cover the projection refactor that replaced silent carry-forward
with honest interpolation + capped forward projection. The previous
behaviour returned the nearest *earlier* observation, which silently
flat-lined the TFP→reference-month ratio whenever BLS hadn't yet
published a CPI point for the reference month.
"""
import importlib.util
import sys
import types
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# tfp-updater.py imports pdfplumber + requests at module scope for the
# scrape path. Neither is needed for the projection unit tests, so we stub
# them in sys.modules to keep the test environment dep-light.
for _name in ("pdfplumber", "requests"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)
# requests.RequestException is referenced in module-level fallbacks
if not hasattr(sys.modules["requests"], "RequestException"):
    sys.modules["requests"].RequestException = Exception  # type: ignore[attr-defined]

_spec = importlib.util.spec_from_file_location("tfp_updater", _ROOT / "tfp-updater.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_cpi_value_for = _mod._cpi_value_for
_PROJ_MONTHLY_CAP = _mod._PROJ_MONTHLY_CAP


def _pt(year, month, value):
    return {"year": year, "month": month, "value": value}


class TestCpiValueFor:
    def test_exact_match(self):
        pts = [_pt(2026, 1, 300.0), _pt(2026, 3, 303.0)]
        assert _cpi_value_for(pts, 2026, 3) == 303.0

    def test_interpolated_between_bimonthly(self):
        """February falls between Jan and Mar bimonthly observations."""
        pts = [_pt(2026, 1, 300.0), _pt(2026, 3, 304.0)]
        # Halfway between → ratio 302.0
        assert _cpi_value_for(pts, 2026, 2) == pytest.approx(302.0, rel=1e-6)

    def test_forward_projection_past_latest(self):
        """Target past latest observation → compound monthly projection."""
        pts = [_pt(2026, 1, 300.0), _pt(2026, 3, 306.0)]  # +1%/mo over 2 mo
        # Project to May (2 months past latest) using monthly_rate ≈ 0.00995
        result = _cpi_value_for(pts, 2026, 5)
        # Should be > 306 but < uncapped continuation
        assert result > 306.0
        assert result < 306.0 * (1 + _PROJ_MONTHLY_CAP) ** 2 + 0.01

    def test_forward_projection_capped_on_noisy_spike(self):
        """A wild bimonthly spike must not blow up the projection."""
        pts = [_pt(2026, 1, 100.0), _pt(2026, 3, 200.0)]  # +100% in 2 months
        result = _cpi_value_for(pts, 2026, 5)
        # Cap = 0.0189/mo → 200 * 1.0189^2 ≈ 207.6, far below uncapped 400
        assert result < 210.0, f"cap not applied: {result}"

    def test_flat_series_projects_flat(self):
        pts = [_pt(2026, 1, 300.0), _pt(2026, 3, 300.0)]
        assert _cpi_value_for(pts, 2026, 5) == pytest.approx(300.0)

    def test_empty_series_returns_none(self):
        assert _cpi_value_for([], 2026, 3) is None

    def test_single_point_returns_that_value(self):
        """One observation: can't compute growth → fall back to that value."""
        pts = [_pt(2026, 3, 300.0)]
        assert _cpi_value_for(pts, 2026, 5) == pytest.approx(300.0)

    def test_target_before_first_observation(self):
        """Target precedes everything → return earliest as least-bad guess."""
        pts = [_pt(2026, 3, 300.0), _pt(2026, 5, 305.0)]
        assert _cpi_value_for(pts, 2026, 1) == pytest.approx(300.0)


class TestProjectTfpForward:
    """The high-level wrapper that uses _cpi_value_for to roll TFP dollars."""

    def test_no_projection_when_tfp_already_at_ref(self):
        """If TFP period >= reference month, no projection needed."""
        result = _mod.project_tfp_forward(
            hi_monthly=1500.0, hi_period="2026-03",
            ref_year=2026, ref_month=3,
        )
        assert result is None
