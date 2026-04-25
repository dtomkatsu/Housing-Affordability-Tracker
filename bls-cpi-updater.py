#!/usr/bin/env python3
"""
bls-cpi-updater.py
------------------
Fetches Honolulu CPI series from BLS and patches the `cpiData` block in both
squarespace-single-file.html and index.html.

Series (area S49A = Urban Hawaii / Honolulu, NSA):
    CUURS49ASA0     — All items, Honolulu (headline)
    CUURS49ASAH     — Shelter, Honolulu
    CUURS49ASAF11   — Food at home, Honolulu
    CUURS49ASETB01  — Gasoline (all types), Honolulu  (energy proxy)
    CUURS49ASAT     — Transportation, Honolulu

Cadence: bimonthly. Data periods are odd months (Jan/Mar/May/Jul/Sep/Nov),
released on or around the 15th of the following even month. YoY is computed
against the same odd-month observation one year prior — both are guaranteed
present in a bimonthly schedule, no interpolation needed.

For each series we compute YoY % change: (latest - 12mo prior) / 12mo prior * 100.

Patch strategy: replace between
    /* CPI_DATA_START */ ... /* CPI_DATA_END */

Env:
    BLS_API_KEY   — optional but recommended (higher rate limits)

Run:
    python3 bls-cpi-updater.py
    python3 bls-cpi-updater.py --dry-run
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add project root + pipelines/grocery to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
GROCERY_ROOT = PROJECT_ROOT / "pipelines" / "grocery"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(GROCERY_ROOT))

from src.cpi_fetcher import fetch_cpi_data      # noqa: E402 (grocery pipeline)
from common.html_patcher import patch_html_files   # noqa: E402

# ---------------------------------------------------------------
SERIES = {
    # Area S49A = Urban Hawaii / Honolulu (CUUR = CPI-U, NSA, bimonthly).
    # Odd-month data periods; see module docstring for release cadence.
    "allItems":  "CUURS49ASA0",     # All items
    "shelter":   "CUURS49ASAH",     # Shelter
    "food":      "CUURS49ASAF11",   # Food at home
    "energy":    "CUURS49ASETB01",  # Gasoline / energy (motor fuel)
    "transport": "CUURS49ASAT",     # Transportation
}

DEFAULT_FILES = [
    PROJECT_ROOT / "squarespace-single-file.html",
    PROJECT_ROOT / "index.html",
]

_DATA_TAG = "CPI"


def compute_yoy(points: list[dict]) -> tuple[float | None, str | None]:
    """Given list of {year, period (M01-M12), value}, return (yoy_pct, latestPeriod 'YYYY-MM')."""
    monthly = [p for p in points if p["period"].startswith("M") and p["period"] != "M13"]
    if not monthly:
        return None, None
    monthly.sort(key=lambda p: (p["year"], int(p["period"][1:])))
    latest = monthly[-1]
    latest_month = int(latest["period"][1:])
    prior_year = latest["year"] - 1
    prior = next(
        (p for p in monthly if p["year"] == prior_year and int(p["period"][1:]) == latest_month),
        None,
    )
    if not prior or prior["value"] == 0:
        return None, f"{latest['year']}-{latest_month:02d}"
    yoy = (latest["value"] - prior["value"]) / prior["value"] * 100.0
    return round(yoy, 2), f"{latest['year']}-{latest_month:02d}"


def build_block(yoy_by_key: dict) -> str:
    lines = ["/* CPI_DATA_START */", "const cpiData = {"]
    pad = max(len(k) for k in SERIES) + 2
    for key, series_id in SERIES.items():
        entry = yoy_by_key.get(key)
        if entry and entry["yoy"] is not None:
            lines.append(
                f'  {(key+":").ljust(pad)}{{ yoy: {entry["yoy"]}, '
                f'latestPeriod: "{entry["latestPeriod"]}", source: "BLS {series_id}" }},'
            )
        else:
            lines.append(f'  {(key+":").ljust(pad)}null,')
    lines.append("};")
    lines.append("/* CPI_DATA_END */")
    return "\n".join(lines)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    files = DEFAULT_FILES
    if "--file" in sys.argv:
        idx = sys.argv.index("--file") + 1
        files = [Path(sys.argv[idx])]

    api_key = os.environ.get("BLS_API_KEY", "")
    if not api_key:
        print("WARNING: BLS_API_KEY not set; using public tier (10 req/day, 2yr window)")

    series_ids = list(SERIES.values())
    try:
        raw = fetch_cpi_data(series_ids, api_key=api_key or None)
    except Exception as e:
        print(f"ERROR fetching BLS: {e}")
        print("Writing null cpiData block (dashboard will hide inflation chips).")
        raw = {sid: [] for sid in series_ids}

    yoy_by_key = {}
    for key, sid in SERIES.items():
        yoy, period = compute_yoy(raw.get(sid, []))
        yoy_by_key[key] = {"yoy": yoy, "latestPeriod": period} if yoy is not None else None
        print(f"  {key:10s} ({sid}): YoY {yoy!r}  latest={period}")

    new_block = build_block(yoy_by_key)
    print("\nNew cpiData block:\n" + new_block + "\n")
    patch_html_files(files, _DATA_TAG, new_block, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
