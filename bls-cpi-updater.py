#!/usr/bin/env python3
"""
bls-cpi-updater.py
------------------
Fetches Honolulu CPI series from BLS and patches the `cpiData` block in both
squarespace-single-file.html and index.html.

Series:
    CUURA426SA0     — All items, Honolulu (headline)
    CUURA426SAH     — Shelter, Honolulu
    CUUSA426SAF11   — Food at home, Honolulu
    CUUSA426SETB01  — Gasoline (all types), Honolulu  (energy proxy)
    CUURA426SAT     — Transportation, Honolulu

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
import re
import sys
from pathlib import Path

# Add pipelines/grocery to sys.path so we can import the existing fetcher
PROJECT_ROOT = Path(__file__).resolve().parent
GROCERY_ROOT = PROJECT_ROOT / "pipelines" / "grocery"
sys.path.insert(0, str(GROCERY_ROOT))

from src.cpi_fetcher import fetch_cpi_data  # noqa: E402

# ---------------------------------------------------------------
SERIES = {
    # Area S49A = Honolulu, HI (CUUR prefix = monthly, not seasonally adjusted)
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

PATCH_RE = re.compile(
    r"/\* CPI_DATA_START \*/.*?/\* CPI_DATA_END \*/",
    flags=re.DOTALL,
)


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


def patch_html(html: str, new_block: str) -> tuple[str, bool]:
    if not PATCH_RE.search(html):
        return html, False
    return PATCH_RE.sub(lambda m: new_block, html, count=1), True


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

    for target in files:
        if not target.exists():
            print(f"skip: {target} not found")
            continue
        html = target.read_text(encoding="utf-8")
        new_html, ok = patch_html(html, new_block)
        if not ok:
            print(f"WARNING: CPI_DATA markers not found in {target}")
            continue
        if dry_run:
            print(f"[dry-run] would patch {target}")
        else:
            target.write_text(new_html, encoding="utf-8")
            print(f"patched {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
