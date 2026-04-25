"""Tests for the AcsObservation / GeographySeries / ForecastPoint dataclasses."""
import pytest

from census_forecasting.src.models import AcsObservation, ForecastPoint, GeographySeries


class TestAcsObservation:
    def test_basic_construction(self):
        o = AcsObservation(
            estimate=100000, moe=2000, year=2024, vintage="1y",
            geoid="15003", indicator="B19013_001E",
        )
        assert o.estimate == 100000
        assert o.year == 2024

    def test_invalid_vintage_raises(self):
        with pytest.raises(ValueError):
            AcsObservation(
                estimate=100, moe=10, year=2024, vintage="3y",
                geoid="15003", indicator="B19013_001E",
            )

    def test_frozen_immutable(self):
        # Pinning the frozen-dataclass contract: callers should not be
        # able to mutate observations downstream of fetch.
        o = AcsObservation(
            estimate=100, moe=10, year=2024, vintage="1y",
            geoid="15003", indicator="X",
        )
        with pytest.raises((AttributeError, TypeError)):
            o.estimate = 200  # type: ignore


class TestGeographySeries:
    def _obs(self, year, vintage="1y", est=100):
        return AcsObservation(
            estimate=est, moe=5, year=year, vintage=vintage,
            geoid="15003", indicator="B19013_001E",
        )

    def test_from_observations_sorts(self):
        unsorted = [self._obs(2020), self._obs(2018), self._obs(2019)]
        s = GeographySeries.from_observations("15003", "B19013_001E", unsorted)
        years = [o.year for o in s.observations]
        assert years == [2018, 2019, 2020]

    def test_from_observations_rejects_mismatched_geoid(self):
        bad = AcsObservation(
            estimate=1, moe=0, year=2020, vintage="1y",
            geoid="15001", indicator="B19013_001E",
        )
        with pytest.raises(ValueError):
            GeographySeries.from_observations("15003", "B19013_001E", [bad])

    def test_from_observations_rejects_duplicates(self):
        # Two 1y observations for the same year would silently corrupt
        # the trend estimation — fail loudly instead.
        with pytest.raises(ValueError):
            GeographySeries.from_observations("15003", "B19013_001E",
                                              [self._obs(2020), self._obs(2020)])

    def test_one_year_filter(self):
        s = GeographySeries.from_observations("15003", "B19013_001E", [
            self._obs(2018, "1y"),
            self._obs(2018, "5y"),
            self._obs(2019, "1y"),
        ])
        assert len(s.one_year()) == 2
        assert all(o.vintage == "1y" for o in s.one_year())

    def test_five_year_filter(self):
        s = GeographySeries.from_observations("15003", "B19013_001E", [
            self._obs(2018, "1y"),
            self._obs(2018, "5y"),
            self._obs(2019, "5y"),
        ])
        assert len(s.five_year()) == 2

    def test_latest_no_observations_returns_none(self):
        s = GeographySeries(geoid="15003", indicator="B19013_001E", observations=[])
        assert s.latest() is None

    def test_latest_with_vintage_filter(self):
        s = GeographySeries.from_observations("15003", "B19013_001E", [
            self._obs(2020, "1y"),
            self._obs(2024, "5y"),
        ])
        assert s.latest("1y").year == 2020
        assert s.latest("5y").year == 2024


class TestForecastPoint:
    def test_basic_fields(self):
        fp = ForecastPoint(
            point=100, se_total=5, se_sample=3, se_forecast=4,
            ci90_low=90, ci90_high=110, method="test",
            target_year=2026, geoid="15003", indicator="B19013_001E",
            horizon=2, notes="example",
        )
        assert fp.point == 100
        assert fp.notes == "example"
