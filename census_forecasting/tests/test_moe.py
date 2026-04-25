"""Tests for MOE conversion + Census Handbook propagation formulas.

Numerical reference values come from worked examples in:
* Census ACS General Handbook, Chapter 8 (2018), "Calculating Measures
  of Error for Derived Estimates"
* Census ACS Accuracy of the Data document (2019, 2018), Worked Examples

Pinning these formulas in tests means a future refactor of the MOE
math has to either match the Census Bureau's documented arithmetic or
explicitly update the tests with the new formula.
"""
import math

import pytest

from census_forecasting.src.moe import (
    ACS_MOE_Z,
    ci_from_se,
    combine_se,
    moe_difference,
    moe_proportion,
    moe_ratio,
    moe_sum,
    moe_to_se,
    relative_se,
    se_to_moe,
)


# ---------------------------------------------------------------------------
# moe_to_se / se_to_moe
# ---------------------------------------------------------------------------

class TestMoeSeConversion:
    def test_moe_to_se_basic(self):
        # MOE 1645 → SE 1000 (the canonical Census Handbook textbook example)
        assert moe_to_se(1645) == pytest.approx(1000.0, rel=1e-6)

    def test_moe_to_se_zero(self):
        assert moe_to_se(0) == 0.0

    def test_moe_to_se_negative_is_nan(self):
        # Census uses negative MOEs to flag suppressed estimates.
        # Returning NaN forces callers to handle the missing case
        # rather than silently propagating a fake SE.
        assert math.isnan(moe_to_se(-1))

    def test_moe_to_se_nan_passthrough(self):
        assert math.isnan(moe_to_se(float("nan")))

    def test_se_to_moe_inverse(self):
        for moe in (100, 1000, 5000, 12345.67):
            assert se_to_moe(moe_to_se(moe)) == pytest.approx(moe, rel=1e-9)

    def test_se_to_moe_negative_raises(self):
        with pytest.raises(ValueError):
            se_to_moe(-1.0)

    def test_z_constant_matches_documented(self):
        # ACS publication standard since 2006.
        assert ACS_MOE_Z == 1.645


# ---------------------------------------------------------------------------
# moe_sum / moe_difference (Handbook 8.1)
# ---------------------------------------------------------------------------

class TestMoeSum:
    def test_two_components(self):
        # Handbook example: combining two MOEs of 100 → sqrt(2)·100 ≈ 141.42
        assert moe_sum([100, 100]) == pytest.approx(math.sqrt(2) * 100, rel=1e-9)

    def test_three_components(self):
        # 3-4-5 triangle generalised: sqrt(9+16+25)=sqrt(50)
        assert moe_sum([3, 4, 5]) == pytest.approx(math.sqrt(50), rel=1e-9)

    def test_difference_equals_sum_of_squares(self):
        # MOE of (a-b) and (a+b) are identical under the Handbook formula.
        assert moe_difference(7, 24) == pytest.approx(moe_sum([7, 24]), rel=1e-9)

    def test_empty_returns_zero(self):
        assert moe_sum([]) == 0.0

    def test_single_component_passthrough(self):
        assert moe_sum([42]) == pytest.approx(42, rel=1e-9)

    def test_nan_in_input_propagates(self):
        # Don't silently treat a missing component as zero — the user
        # should know one of their inputs has no MOE.
        assert math.isnan(moe_sum([100, float("nan")]))

    def test_negative_component_returns_nan(self):
        # Census uses negative MOE as a sentinel; can't sum sentinels.
        assert math.isnan(moe_sum([100, -1]))


# ---------------------------------------------------------------------------
# moe_ratio (Handbook 8.3, the general two-independent-estimates ratio)
# ---------------------------------------------------------------------------

class TestMoeRatio:
    def test_handbook_worked_example(self):
        # Handbook Ch 8 worked example (slightly adapted):
        # num = 50, den = 100, MOE_num = 10, MOE_den = 5
        # R = 0.5
        # MOE_R = (1/100) * sqrt(10² + 0.5²·5²) = (1/100)·sqrt(100+6.25)
        #       = sqrt(106.25)/100 ≈ 0.10308
        moe = moe_ratio(50, 100, 10, 5)
        expected = math.sqrt(100 + 0.25 * 25) / 100
        assert moe == pytest.approx(expected, rel=1e-9)

    def test_zero_denominator_returns_nan(self):
        assert math.isnan(moe_ratio(50, 0, 10, 5))

    def test_nonfinite_denominator_returns_nan(self):
        assert math.isnan(moe_ratio(50, float("inf"), 10, 5))

    def test_nan_input_returns_nan(self):
        assert math.isnan(moe_ratio(50, 100, float("nan"), 5))

    def test_negative_moe_returns_nan(self):
        assert math.isnan(moe_ratio(50, 100, -1, 5))


# ---------------------------------------------------------------------------
# moe_proportion (Handbook 8.2 — special case where num ⊂ den)
# ---------------------------------------------------------------------------

class TestMoeProportion:
    def test_proportion_smaller_than_ratio(self):
        # When the radical stays positive, proportion MOE is *smaller*
        # than the ratio MOE because covariance is implicit (num
        # contributes to den). This is a defining property of the
        # proportion formula and worth pinning.
        num, den = 30, 100
        moe_n, moe_d = 5, 10
        prop_moe = moe_proportion(num, den, moe_n, moe_d)
        ratio_moe = moe_ratio(num, den, moe_n, moe_d)
        assert prop_moe < ratio_moe

    def test_negative_radical_falls_back_to_ratio(self):
        # When MOE_num² < P²·MOE_den² the Handbook prescribes falling
        # back to the ratio formula. We pin that fallback here.
        num, den = 90, 100  # P = 0.9, very close to 1
        moe_n, moe_d = 1, 30  # tiny num MOE, huge den MOE
        prop_moe = moe_proportion(num, den, moe_n, moe_d)
        ratio_moe = moe_ratio(num, den, moe_n, moe_d)
        # Both should equal the ratio formula in this regime.
        assert prop_moe == pytest.approx(ratio_moe, rel=1e-9)

    def test_zero_denominator_is_nan(self):
        assert math.isnan(moe_proportion(0, 0, 0, 0))

    def test_proportion_at_zero_part_equals_num_only(self):
        # P=0: MOE_P = MOE_num / den (the den-uncertainty term zeros out)
        num, den = 0, 100
        moe_n, moe_d = 8, 12
        assert moe_proportion(num, den, moe_n, moe_d) == pytest.approx(8 / 100, rel=1e-9)


# ---------------------------------------------------------------------------
# combine_se / ci_from_se
# ---------------------------------------------------------------------------

class TestCombineSe:
    def test_quadrature(self):
        # SEs combine in quadrature under independence.
        assert combine_se(3, 4) == pytest.approx(5.0, rel=1e-9)

    def test_single(self):
        assert combine_se(7) == pytest.approx(7, rel=1e-9)

    def test_with_zero(self):
        assert combine_se(0, 5) == pytest.approx(5, rel=1e-9)

    def test_nan_propagates(self):
        assert math.isnan(combine_se(3, float("nan")))

    def test_negative_returns_nan(self):
        # Defensive: SE is non-negative by definition; a negative input
        # is a bug upstream — flag it as NaN rather than silently take
        # the absolute value.
        assert math.isnan(combine_se(3, -1))


class TestCiFromSe:
    def test_default_z_is_acs_90pct(self):
        lo, hi = ci_from_se(100, 10)
        assert hi - lo == pytest.approx(2 * 1.645 * 10, rel=1e-9)

    def test_custom_z_95pct(self):
        lo, hi = ci_from_se(100, 10, z=1.96)
        assert hi - lo == pytest.approx(2 * 1.96 * 10, rel=1e-9)

    def test_symmetric_around_point(self):
        lo, hi = ci_from_se(50, 5)
        assert (lo + hi) / 2 == pytest.approx(50, rel=1e-9)

    def test_nan_se_returns_nan_bounds(self):
        lo, hi = ci_from_se(50, float("nan"))
        assert math.isnan(lo) and math.isnan(hi)


class TestRelativeSe:
    def test_basic(self):
        # MOE 1645 / 1.645 = SE 1000; estimate 5000 → CV = 0.2
        assert relative_se(5000, 1645) == pytest.approx(0.2, rel=1e-9)

    def test_zero_estimate_is_nan(self):
        assert math.isnan(relative_se(0, 100))

    def test_nan_moe_propagates(self):
        assert math.isnan(relative_se(1000, float("nan")))
