"""Unit tests for pipelines/grocery/src/pumd_extractor.py.

Uses synthetic, PUMD-shaped DataFrames to exercise:
  - is_fah_ucc()             — UCC hierarchy filter
  - family_size_bucket()     — stratification logic
  - extract_honolulu_fah()   — PSU filter, MTBI aggregation, FINLWT21 weighting
  - pool_years()             — multi-year combination
  - project_to_neighbor_islands() — basket-gradient scaling

No PUMD download required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")  # gracefully skip if pandas is not installed

# pumd_extractor lives in pipelines/grocery/src/
_GROCERY_ROOT = Path(__file__).resolve().parent.parent / "pipelines" / "grocery"
if str(_GROCERY_ROOT) not in sys.path:
    sys.path.insert(0, str(_GROCERY_ROOT))

from src.pumd_extractor import (   # noqa: E402
    extract_honolulu_fah,
    family_size_bucket,
    is_fah_ucc,
    pool_years,
    project_to_neighbor_islands,
    FAHResult,
    HONOLULU_PSU_CODES,
)


# ---------------------------------------------------------------------------
# UCC filter
# ---------------------------------------------------------------------------
class TestIsFahUcc:
    def test_root_19_is_fah(self):
        assert is_fah_ucc("190101")
        assert is_fah_ucc(190101)        # int form too
        assert is_fah_ucc("190201")
        assert is_fah_ucc("191001")       # other 19xx subtrees

    def test_1909_is_excluded(self):
        """1909* is 'food at home on trips' — must not be aggregated."""
        assert not is_fah_ucc("190901")
        assert not is_fah_ucc("190910")
        assert not is_fah_ucc(190999)

    def test_food_away_excluded(self):
        """20* is food away from home — must not be aggregated."""
        assert not is_fah_ucc("200111")
        assert not is_fah_ucc("210110")
        assert not is_fah_ucc("999999")


# ---------------------------------------------------------------------------
# Family-size bucketing
# ---------------------------------------------------------------------------
class TestFamilySizeBucket:
    def test_singleton_bucket(self):
        assert family_size_bucket(1) == "1"
        assert family_size_bucket(0) == "1"     # degenerate but defensible
        assert family_size_bucket(-3) == "1"

    def test_pair(self):
        assert family_size_bucket(2) == "2"
        assert family_size_bucket(2.0) == "2"

    def test_triple(self):
        assert family_size_bucket(3) == "3"

    def test_four_plus_bucket(self):
        assert family_size_bucket(4) == "4+"
        assert family_size_bucket(7) == "4+"
        assert family_size_bucket(99) == "4+"


# ---------------------------------------------------------------------------
# extract_honolulu_fah — synthetic PUMD-shaped DataFrames
# ---------------------------------------------------------------------------
def _make_synthetic(psu_codes: dict[int, str], fam_sizes: dict[int, int],
                    weights: dict[int, float], fah_per_cu: dict[int, float],
                    food_away_per_cu: dict[int, float] | None = None) -> tuple:
    """Build synthetic FMLI + MTBI DataFrames keyed by NEWID."""
    fmli = pd.DataFrame({
        "NEWID":     list(psu_codes.keys()),
        "PSU":       [psu_codes[k] for k in psu_codes],
        "FAM_SIZE":  [fam_sizes[k] for k in psu_codes],
        "FINLWT21":  [weights[k] for k in psu_codes],
    })
    rows = []
    for newid, total in fah_per_cu.items():
        # split evenly across two FAH UCCs (1901xx and 1902xx)
        rows.append({"NEWID": newid, "UCC": "190101", "COST": total / 2.0})
        rows.append({"NEWID": newid, "UCC": "190201", "COST": total / 2.0})
    if food_away_per_cu:
        for newid, total in food_away_per_cu.items():
            rows.append({"NEWID": newid, "UCC": "200111", "COST": total})
    mtbi = pd.DataFrame(rows)
    return fmli, mtbi


class TestExtractHonoluluFah:
    def test_psu_filter_keeps_only_honolulu(self):
        """CUs with non-Honolulu PSU should not contribute."""
        psu = {1: "S49A", 2: "S49A", 3: "ZZZZ"}      # 3 is non-Honolulu
        fam = {1: 4, 2: 4, 3: 4}
        wts = {1: 1.0, 2: 1.0, 3: 1.0}
        fah = {1: 600.0, 2: 600.0, 3: 9999.0}        # outlier in non-Honolulu CU
        fmli, mtbi = _make_synthetic(psu, fam, wts, fah)
        result = extract_honolulu_fah(fmli, mtbi, fmli_year=2023)
        # Quarterly $600 → monthly $200; mean over the two retained CUs
        assert result["overall"].monthly_fah == pytest.approx(200.0)
        assert result["n_total"] == 2

    def test_food_away_excluded(self):
        """20* spending must not appear in the FAH total."""
        psu = {1: "S49A"}
        fam = {1: 2}
        wts = {1: 1.0}
        fah = {1: 300.0}        # 300 quarterly = 100 monthly
        away = {1: 999.0}       # large food-away — must be ignored
        fmli, mtbi = _make_synthetic(psu, fam, wts, fah, food_away_per_cu=away)
        result = extract_honolulu_fah(fmli, mtbi, fmli_year=2023)
        assert result["overall"].monthly_fah == pytest.approx(100.0)

    def test_finlwt21_weighting(self):
        """A CU with double the weight should pull the mean toward its value."""
        psu = {1: "S49A", 2: "S49A"}
        fam = {1: 4, 2: 4}
        wts = {1: 1.0, 2: 9.0}
        # CU1 quarterly $300 → $100/mo; CU2 quarterly $900 → $300/mo
        fah = {1: 300.0, 2: 900.0}
        fmli, mtbi = _make_synthetic(psu, fam, wts, fah)
        result = extract_honolulu_fah(fmli, mtbi, fmli_year=2023)
        # Weighted mean: (1*100 + 9*300) / 10 = 280
        assert result["overall"].monthly_fah == pytest.approx(280.0)

    def test_quarterly_to_monthly_division(self):
        """A CU with $600 quarterly FAH → $200 monthly."""
        psu = {1: "S49A"}
        fam = {1: 3}
        wts = {1: 1.0}
        fah = {1: 600.0}
        fmli, mtbi = _make_synthetic(psu, fam, wts, fah)
        result = extract_honolulu_fah(fmli, mtbi, fmli_year=2023)
        assert result["overall"].monthly_fah == pytest.approx(200.0)

    def test_inflation_adjustment(self):
        """If from_year=2020 cost is $200/mo and food CPI doubled by 2024,
        result should report $400/mo (target_year=2024)."""
        psu = {1: "S49A"}
        fam = {1: 2}
        wts = {1: 1.0}
        fah = {1: 600.0}                  # $200/mo
        fmli, mtbi = _make_synthetic(psu, fam, wts, fah)
        result = extract_honolulu_fah(
            fmli, mtbi, fmli_year=2020,
            food_cpi_annual={2020: 100.0, 2024: 200.0},
            target_year=2024,
        )
        assert result["overall"].monthly_fah == pytest.approx(400.0)

    def test_size_buckets_split_correctly(self):
        psu = {1: "S49A", 2: "S49A", 3: "S49A", 4: "S49A"}
        fam = {1: 1, 2: 2, 3: 3, 4: 5}
        wts = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}
        fah = {1: 300.0, 2: 600.0, 3: 900.0, 4: 1200.0}
        fmli, mtbi = _make_synthetic(psu, fam, wts, fah)
        result = extract_honolulu_fah(fmli, mtbi, fmli_year=2023)
        assert result["by_size"]["1"].monthly_fah  == pytest.approx(100.0)
        assert result["by_size"]["2"].monthly_fah  == pytest.approx(200.0)
        assert result["by_size"]["3"].monthly_fah  == pytest.approx(300.0)
        assert result["by_size"]["4+"].monthly_fah == pytest.approx(400.0)

    def test_no_honolulu_returns_empty(self):
        psu = {1: "ZZZZ"}
        fam = {1: 4}
        wts = {1: 1.0}
        fah = {1: 600.0}
        fmli, mtbi = _make_synthetic(psu, fam, wts, fah)
        result = extract_honolulu_fah(fmli, mtbi, fmli_year=2023)
        assert result["n_total"] == 0
        assert result["overall"].monthly_fah == 0.0

    def test_known_honolulu_psu_codes_match(self):
        """All registered Honolulu PSU codes must trigger inclusion."""
        for code in HONOLULU_PSU_CODES:
            psu = {1: code}
            fam = {1: 2}
            wts = {1: 1.0}
            fah = {1: 300.0}
            fmli, mtbi = _make_synthetic(psu, fam, wts, fah)
            result = extract_honolulu_fah(fmli, mtbi, fmli_year=2023)
            assert result["n_total"] == 1, f"PSU code {code} not matched"


# ---------------------------------------------------------------------------
# pool_years
# ---------------------------------------------------------------------------
class TestPoolYears:
    def test_pooled_mean_is_n_weighted(self):
        """Pooled mean weights by per-year n_households."""
        per_year = [
            {
                "year": 2019,
                "n_total": 100,
                "overall": FAHResult(100.0, 100, (90.0, 110.0), "all"),
                "by_size": {b: FAHResult(100.0, 100, (90.0, 110.0), b)
                            for b in ("1", "2", "3", "4+")},
            },
            {
                "year": 2023,
                "n_total": 300,
                "overall": FAHResult(200.0, 300, (190.0, 210.0), "all"),
                "by_size": {b: FAHResult(200.0, 300, (190.0, 210.0), b)
                            for b in ("1", "2", "3", "4+")},
            },
        ]
        pooled = pool_years(per_year)
        # (100 * 100 + 300 * 200) / 400 = 175
        assert pooled["overall"].monthly_fah == pytest.approx(175.0)
        assert pooled["n_total"] == 400
        assert pooled["years"] == [2019, 2023]

    def test_empty_input_raises(self):
        with pytest.raises(ValueError):
            pool_years([])


# ---------------------------------------------------------------------------
# project_to_neighbor_islands
# ---------------------------------------------------------------------------
class TestNeighborIslandProjection:
    def test_basket_gradient_applied(self):
        """county = honolulu × (basket[county] / basket[honolulu])."""
        baskets = {"Honolulu": 100.0, "Maui": 110.0, "Hawaii": 95.0, "Kauai": 105.0}
        out = project_to_neighbor_islands(1000.0, baskets)
        assert out["Honolulu"] == pytest.approx(1000.0)
        assert out["Maui"]     == pytest.approx(1100.0)
        assert out["Hawaii"]   == pytest.approx(950.0)
        assert out["Kauai"]    == pytest.approx(1050.0)

    def test_state_is_population_weighted_mean(self):
        baskets = {"Honolulu": 100.0, "Maui": 100.0, "Hawaii": 100.0, "Kauai": 100.0}
        out = project_to_neighbor_islands(1000.0, baskets)
        # All counties equal → state should also be 1000
        assert out["State"] == pytest.approx(1000.0)

    def test_state_pulls_toward_honolulu(self):
        """When all counties are equal at value V, state == V; when neighbors
        differ, the state value sits between Honolulu and the neighbor mean,
        weighted heavily toward Honolulu (~70% of population)."""
        baskets = {"Honolulu": 100.0, "Maui": 200.0, "Hawaii": 200.0, "Kauai": 200.0}
        out = project_to_neighbor_islands(1000.0, baskets)
        # Honolulu: 1000, neighbors: 2000 each. With Honolulu at ~70% pop weight,
        # state should be closer to 1000 than 2000.
        assert 1000.0 < out["State"] < 1500.0

    def test_missing_basket_raises(self):
        with pytest.raises(ValueError, match="Honolulu"):
            project_to_neighbor_islands(1000.0, {"Maui": 100.0})

    def test_missing_neighbor_skipped(self):
        baskets = {"Honolulu": 100.0, "Maui": 110.0}   # Hawaii, Kauai missing
        out = project_to_neighbor_islands(1000.0, baskets)
        assert "Hawaii" not in out
        assert "Kauai" not in out
        assert out["Honolulu"] == pytest.approx(1000.0)
        assert out["Maui"]     == pytest.approx(1100.0)
        assert "State" in out
