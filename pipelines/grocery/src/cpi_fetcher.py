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
            raw_val = obs.get("value", "-")
            if raw_val == "-":
                continue  # BLS uses '-' for missing/preliminary data
            points.append({
                "year": int(obs["year"]),
                "period": obs["period"],  # e.g. "M01", "M02", ... "M12"
                "value": float(raw_val),
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


# ---------------------------------------------------------------------------
# Staleness helpers (required by pipeline.py)
# ---------------------------------------------------------------------------

# Honolulu (area S49A) CPI is bimonthly. The *data periods* are odd months
# (Jan, Mar, May, Jul, Sep, Nov) and the release lands the following even
# month around the 15th — e.g. Mar-2026 data is published on or around
# Apr-15, 2026. A previous version of this constant listed the *release*
# months (even), which made expected_latest_period() ask the cache for
# data periods that BLS never publishes (e.g. Feb), forcing a refetch on
# every run. Verify against the BLS Honolulu schedule before changing:
#   https://www.bls.gov/regions/west/news-release/consumerpriceindex_honolulu.htm
BLS_DATA_MONTHS = {1, 3, 5, 7, 9, 11}
BLS_RELEASE_DAY = 15


def expected_latest_period(today: date | None = None) -> tuple[int, int]:
    """Return (year, month) of the most recent bimonthly Honolulu CPI data
    period expected to have been released by *today*.

    Honolulu CPI data periods are odd months; each is released on or around
    the 15th of the *following* month. So the Mar-2026 data point only counts
    as "expected" once today >= 2026-04-15.
    """
    if today is None:
        today = date.today()
    y, m = today.year, today.month
    for _ in range(12):
        if m in BLS_DATA_MONTHS:
            release_year  = y if m < 12 else y + 1
            release_month = m + 1 if m < 12 else 1
            if (today.year, today.month, today.day) >= (release_year, release_month, BLS_RELEASE_DAY):
                return (y, m)
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return (today.year - 1, today.month)


def cache_has_period(cpi_data: dict, series_ids: list[str], year: int, month: int) -> bool:
    """True if every series in *series_ids* has a data point for year/M{month:02d}."""
    period = f"M{month:02d}"
    for sid in series_ids:
        points = cpi_data.get(sid, [])
        if not any(p["year"] == year and p["period"] == period for p in points):
            return False
    return True


def fetch_if_stale(
    cpi_config: CPIConfig,
    start_year: int | None = None,
    api_key: str | None = None,
) -> tuple[dict, bool]:
    """Fetch BLS CPI only if the cache lacks the most recent expected period.

    Returns *(cpi_data, did_fetch)*. Falls back to cached data on network error.
    """
    cached = load_cached_cpi() or {}
    series_ids = cpi_config.all_series_ids
    exp_year, exp_month = expected_latest_period()

    if cached and cache_has_period(cached, series_ids, exp_year, exp_month):
        return cached, False

    try:
        fresh = fetch_and_cache(cpi_config, start_year=start_year, api_key=api_key)
        return fresh, True
    except Exception:
        return cached, False
