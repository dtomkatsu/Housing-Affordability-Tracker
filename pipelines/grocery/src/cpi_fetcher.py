"""BLS API client for fetching Honolulu CPI data."""

import json
import os
from datetime import date
from pathlib import Path

import requests

from .models import CPIConfig


CACHE_DIR = Path(__file__).parent.parent / "data" / "cpi_cache"


def fetch_cpi_data(
    series_ids: list[str],
    start_year: int | None = None,
    end_year: int | None = None,
    api_key: str | None = None,
) -> dict:
    """Fetch CPI time series from BLS API v2.

    Returns dict mapping series_id -> list of {year, period, value} dicts.
    """
    if api_key is None:
        api_key = os.environ.get("BLS_API_KEY", "")

    if start_year is None:
        start_year = date.today().year - 1
    if end_year is None:
        end_year = date.today().year

    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    payload = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    if api_key:
        payload["registrationkey"] = api_key

    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API error: {data.get('message', data)}")

    results = {}
    for series in data.get("Results", {}).get("series", []):
        sid = series["seriesID"]
        points = []
        for obs in series.get("data", []):
            points.append({
                "year": int(obs["year"]),
                "period": obs["period"],  # e.g. "M01", "M02", ... "M12"
                "value": float(obs["value"]),
            })
        # Sort chronologically (BLS returns newest first)
        points.sort(key=lambda p: (p["year"], p["period"]))
        results[sid] = points

    return results


def fetch_and_cache(
    cpi_config: CPIConfig,
    start_year: int | None = None,
    end_year: int | None = None,
    api_key: str | None = None,
) -> dict:
    """Fetch all configured CPI series and cache to disk."""
    series_ids = cpi_config.all_series_ids
    data = fetch_cpi_data(series_ids, start_year, end_year, api_key)

    # Cache results
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"cpi_{date.today().isoformat()}.json"
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)

    return data


def load_cached_cpi() -> dict | None:
    """Load the most recent cached CPI data, if any."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_files = sorted(CACHE_DIR.glob("cpi_*.json"), reverse=True)
    if not cache_files:
        return None
    with open(cache_files[0]) as f:
        return json.load(f)


def get_cpi_value(cpi_data: dict, series_id: str, year: int, period: str) -> float | None:
    """Get a specific CPI value from fetched data."""
    points = cpi_data.get(series_id, [])
    for p in points:
        if p["year"] == year and p["period"] == period:
            return p["value"]
    return None


def get_latest_cpi(cpi_data: dict, series_id: str) -> dict | None:
    """Get the most recent CPI data point for a series."""
    points = cpi_data.get(series_id, [])
    if not points:
        return None
    return points[-1]


def date_to_bls_period(d: date) -> tuple[int, str]:
    """Convert a date to BLS year and period (e.g. M04 for April)."""
    return d.year, f"M{d.month:02d}"


def find_nearest_periods(cpi_data: dict, series_id: str, target_year: int, target_month: int) -> tuple[dict | None, dict | None]:
    """Find the two CPI periods bracketing a target month (for interpolation).

    BLS Honolulu CPI is bimonthly. Returns (before, after) data points,
    or (exact, None) if the target month has data.
    """
    points = cpi_data.get(series_id, [])
    if not points:
        return None, None

    target_period = f"M{target_month:02d}"

    # Check for exact match
    for p in points:
        if p["year"] == target_year and p["period"] == target_period:
            return p, None

    # Find bracketing points
    before = None
    after = None
    for p in points:
        p_month = int(p["period"][1:])
        p_val = p["year"] * 12 + p_month
        target_val = target_year * 12 + target_month

        if p_val <= target_val:
            if before is None or p_val > before["year"] * 12 + int(before["period"][1:]):
                before = p
        if p_val >= target_val:
            if after is None or p_val < after["year"] * 12 + int(after["period"][1:]):
                after = p

    return before, after
