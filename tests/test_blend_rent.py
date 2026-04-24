"""Unit tests for blend_rent_nowcast() in redfin-price-updater.py.

These tests cover the rent blending math which feeds directly into the
dashboard's rent figure — the highest-trust computation in the pipeline.
"""
import importlib.util
import sys
from pathlib import Path
import pytest

# Load redfin-price-updater.py as a module (no .py suffix makes standard
# import tricky; importlib handles it cleanly).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_spec = importlib.util.spec_from_file_location("rpu", _ROOT / "redfin-price-updater.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

blend_rent_nowcast = _mod.blend_rent_nowcast
BLENDED_RENT_CPI_WEIGHT = _mod.BLENDED_RENT_CPI_WEIGHT


class TestBlendRentNowcast:
    def test_returns_all_keys(self):
        result = blend_rent_nowcast(acs_anchor=1898, bls_ratio=1.05, zori_ratio=1.10)
        for key in ("blended", "cpi_scaled", "zori_implied", "bls_ratio", "zori_ratio", "cpi_weight"):
            assert key in result, f"missing key: {key}"

    def test_blended_is_weighted_average(self):
        anchor, bls, zori = 2000, 1.04, 1.08
        result = blend_rent_nowcast(acs_anchor=anchor, bls_ratio=bls, zori_ratio=zori)
        w = BLENDED_RENT_CPI_WEIGHT
        expected = round(anchor * (w * bls + (1 - w) * zori))
        assert result["blended"] == expected

    def test_equal_ratios_blended_equals_cpi_and_zori(self):
        """When both ratios are equal the blend equals each component."""
        result = blend_rent_nowcast(acs_anchor=1000, bls_ratio=1.05, zori_ratio=1.05)
        assert result["blended"] == result["cpi_scaled"] == result["zori_implied"]

    def test_bls_only_weight(self):
        """weight=1.0 should produce blended == cpi_scaled."""
        result = blend_rent_nowcast(acs_anchor=2000, bls_ratio=1.06, zori_ratio=1.12, cpi_weight=1.0)
        assert result["blended"] == result["cpi_scaled"]

    def test_zori_only_weight(self):
        """weight=0.0 should produce blended == zori_implied."""
        result = blend_rent_nowcast(acs_anchor=2000, bls_ratio=1.06, zori_ratio=1.12, cpi_weight=0.0)
        assert result["blended"] == result["zori_implied"]

    def test_70_30_default_weight(self):
        """Default weight should be 0.7 (70% CPI)."""
        result = blend_rent_nowcast(acs_anchor=1000, bls_ratio=1.0, zori_ratio=1.0)
        assert result["cpi_weight"] == pytest.approx(0.7)

    def test_anchor_scales_proportionally(self):
        """Doubling the anchor should double the blended rent."""
        r1 = blend_rent_nowcast(acs_anchor=1000, bls_ratio=1.05, zori_ratio=1.05)
        r2 = blend_rent_nowcast(acs_anchor=2000, bls_ratio=1.05, zori_ratio=1.05)
        assert r2["blended"] == r1["blended"] * 2

    def test_unit_ratio_returns_anchor(self):
        """Both ratios at 1.0 → blended should equal the anchor."""
        anchor = 1898
        result = blend_rent_nowcast(acs_anchor=anchor, bls_ratio=1.0, zori_ratio=1.0)
        assert result["blended"] == anchor

    def test_cpi_scaled_uses_bls_only(self):
        anchor, bls, zori = 1500, 1.07, 1.15
        result = blend_rent_nowcast(acs_anchor=anchor, bls_ratio=bls, zori_ratio=zori)
        assert result["cpi_scaled"] == round(anchor * bls)

    def test_zori_implied_uses_zori_only(self):
        anchor, bls, zori = 1500, 1.07, 1.15
        result = blend_rent_nowcast(acs_anchor=anchor, bls_ratio=bls, zori_ratio=zori)
        assert result["zori_implied"] == round(anchor * zori)

    def test_custom_weight_applied(self):
        result = blend_rent_nowcast(acs_anchor=2000, bls_ratio=1.0, zori_ratio=1.1, cpi_weight=0.5)
        assert result["blended"] == round(2000 * (0.5 * 1.0 + 0.5 * 1.1))
        assert result["cpi_weight"] == pytest.approx(0.5)
