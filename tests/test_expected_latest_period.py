"""Unit tests for cpi_fetcher.expected_latest_period().

Locks in the odd-month bimonthly cadence for Honolulu CPI (area S49A).
A previous version of BLS_RELEASE_MONTHS listed the *release* months
(even) instead of the *data* months (odd), which made the staleness check
ask for data periods BLS never publishes — every run silently re-fetched.
"""
import sys
from datetime import date
from pathlib import Path

_GROCERY_ROOT = Path(__file__).resolve().parent.parent / "pipelines" / "grocery"
if str(_GROCERY_ROOT) not in sys.path:
    sys.path.insert(0, str(_GROCERY_ROOT))

from src.cpi_fetcher import (  # noqa: E402
    BLS_DATA_MONTHS,
    expected_latest_period,
)


class TestBlsDataMonthsConstant:
    def test_data_months_are_odd(self):
        """Honolulu bimonthly CPI publishes for Jan/Mar/May/Jul/Sep/Nov."""
        assert BLS_DATA_MONTHS == {1, 3, 5, 7, 9, 11}


class TestExpectedLatestPeriod:
    def test_after_release_returns_that_period(self):
        """Mar-2026 data is released ~Apr-15, 2026; querying Apr 16 returns Mar."""
        assert expected_latest_period(date(2026, 4, 16)) == (2026, 3)

    def test_before_release_returns_prior_period(self):
        """On Apr 14 the Mar-2026 release hasn't landed; latest is Jan-2026."""
        assert expected_latest_period(date(2026, 4, 14)) == (2026, 1)

    def test_release_day_inclusive(self):
        """The 15th itself counts as 'released' (>= comparison)."""
        assert expected_latest_period(date(2026, 4, 15)) == (2026, 3)

    def test_mid_odd_month_returns_prior_odd_period(self):
        """Mid-March 2026: Mar data not yet released → latest is Jan-2026."""
        assert expected_latest_period(date(2026, 3, 15)) == (2026, 1)

    def test_january_walks_back_into_prior_year(self):
        """Early January: Nov-prior-year data is the most recent release."""
        assert expected_latest_period(date(2026, 1, 5)) == (2025, 11)

    def test_december_after_nov_release(self):
        """Late December: Nov-current-year released ~Dec-15."""
        assert expected_latest_period(date(2026, 12, 20)) == (2026, 11)

    def test_never_returns_even_month(self):
        """Across a full year of query days, the result is always an odd month."""
        for month in range(1, 13):
            for day in (1, 15, 28):
                y, m = expected_latest_period(date(2026, month, day))
                assert m % 2 == 1, f"got even data month {m} for query {2026}-{month:02d}-{day:02d}"
