"""Tests for the ACS API client — cache behavior and parsing.

These tests run entirely offline against the on-disk cache that the
back-test populated. They do not hit the network. CI environments
without internet should still be able to run them.
"""
import json

import pytest

from census_forecasting.src.acs_client import AcsClient, SUSPENDED_ONE_YEAR


class TestCacheBehavior:
    def test_uses_cache_when_present(self, tmp_path):
        # Pre-populate cache with a synthetic entry.
        cache_path = tmp_path / "acs_cache.json"
        synthetic_key = "1y|2024|for=county:003&in=state:15|B19013_001E,B19013_001M"
        synthetic_value = [{
            "NAME": "Honolulu County, Hawaii",
            "B19013_001E": "105205",
            "B19013_001M": "3089",
            "state": "15",
            "county": "003",
        }]
        cache_path.write_text(json.dumps({synthetic_key: synthetic_value}))

        # offline=True forces RuntimeError on any network access.
        client = AcsClient(cache_path=cache_path, offline=True)
        rows = client.fetch_table(
            year=2024, vintage="1y",
            indicators=("B19013_001E", "B19013_001M"),
            geo_scope="for=county:003&in=state:15",
        )
        assert len(rows) == 1
        assert rows[0]["B19013_001E"] == "105205"

    def test_offline_with_empty_cache_raises(self, tmp_path):
        client = AcsClient(cache_path=tmp_path / "missing.json", offline=True)
        with pytest.raises(RuntimeError, match="offline"):
            client.fetch_table(
                year=2024, vintage="1y",
                indicators=("B19013_001E",),
                geo_scope="for=county:001&in=state:15",
            )

    def test_suspended_2020_returns_empty(self, tmp_path):
        # 2020 1-year ACS was suspended (COVID); the client treats
        # any 2020 1-year request as empty without hitting the network.
        client = AcsClient(cache_path=tmp_path / "x.json", offline=True)
        rows = client.fetch_table(
            year=2020, vintage="1y",
            indicators=("B19013_001E",),
            geo_scope="for=county:003&in=state:15",
        )
        assert rows == []

    def test_invalid_vintage_raises(self, tmp_path):
        client = AcsClient(cache_path=tmp_path / "x.json", offline=True)
        with pytest.raises(ValueError):
            client.fetch_table(
                year=2024, vintage="2y",
                indicators=("X",),
                geo_scope="",
            )


class TestFetchSeriesParsing:
    def test_parses_response_into_observations(self, tmp_path):
        cache_path = tmp_path / "acs_cache.json"
        # One row per year in the cache, simulating a multi-year fetch.
        cache = {}
        for yr in (2022, 2023, 2024):
            key = f"1y|{yr}|for=county:*&in=state:15|B19013_001E,B19013_001M"
            cache[key] = [{
                "NAME": "Honolulu County, Hawaii",
                "B19013_001E": str(100000 + yr * 100),
                "B19013_001M": "2500",
                "state": "15", "county": "003",
            }]
        cache_path.write_text(json.dumps(cache))

        client = AcsClient(cache_path=cache_path, offline=True)
        obs = client.fetch_series(
            indicator="B19013_001E",
            years=(2022, 2023, 2024),
            vintage="1y",
            state_fips="15",
        )
        assert len(obs) == 3
        assert all(o.geoid == "15003" for o in obs)
        assert all(o.vintage == "1y" for o in obs)
        # Sentinel-coded estimates would be filtered. Sanity-check the
        # year mapping landed on the right rows.
        years = sorted(o.year for o in obs)
        assert years == [2022, 2023, 2024]

    def test_filters_census_sentinel_estimates(self, tmp_path):
        # Census flags suppressed cells with -666666666 etc.
        cache_path = tmp_path / "acs_cache.json"
        key = "1y|2024|for=county:*&in=state:15|B19013_001E,B19013_001M"
        cache_path.write_text(json.dumps({key: [{
            "NAME": "Mystery County",
            "B19013_001E": "-666666666",
            "B19013_001M": "-1",
            "state": "15", "county": "999",
        }]}))
        client = AcsClient(cache_path=cache_path, offline=True)
        obs = client.fetch_series(
            indicator="B19013_001E",
            years=(2024,),
            vintage="1y",
            state_fips="15",
        )
        # Sentinel-coded row dropped.
        assert obs == []


class TestSuspendedConstant:
    def test_2020_in_suspended_set(self):
        # Pin the constant — if Census ever resumes 2020 1-year (they
        # won't, but) and we update the package to re-fetch it, this
        # test forces a deliberate change.
        assert 2020 in SUSPENDED_ONE_YEAR
