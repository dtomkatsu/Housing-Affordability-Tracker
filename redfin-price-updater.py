#!/usr/bin/env python3
"""
Redfin + Zillow + DBEDT → Housing Affordability Tracker price updater.

Downloads:
  - Redfin public S3 TSV files → median sale prices (SFH + condo) per county
  - Zillow ZORI county CSV     → median asking rent per county
  - DBEDT QSER construction XLSX → private building authorization values per county

Patches squarespace-single-file.html in-place with fresh values.

No API keys needed — all sources are public.
Redfin data: monthly (Friday of the third full week).
Zillow ZORI: monthly.
DBEDT QSER:  quarterly.

Usage:
    python3 redfin-price-updater.py                   # update squarespace-single-file.html
    python3 redfin-price-updater.py --dry-run          # print changes without writing
    python3 redfin-price-updater.py --file other.html  # target a different file

Sources:
  Redfin:  https://www.redfin.com/news/data-center/
  Zillow:  https://www.zillow.com/research/data/
  DBEDT:   https://dbedt.hawaii.gov/economic/qser/construction/
"""

import csv
import gzip
import io
import re
import sys
import urllib.request
from pathlib import Path

try:
    import openpyxl
    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False

# ─── CONFIG ─────────────────────────────────────────────────────
STATE_URL  = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/state_market_tracker.tsv000.gz"
COUNTY_URL = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/county_market_tracker.tsv000.gz"
ZORI_URL   = "https://files.zillowstatic.com/research/public_csvs/zori/County_zori_uc_sfrcondomfr_sm_month.csv"
DBEDT_URL  = "https://files.hawaii.gov/dbedt/economic/data_reports/qser/E-construction-tables.xlsx"

# DBEDT E-8 column header → countyData key (columns in order: State, Honolulu, Hawaii, Kauai, Maui)
# The header row in the sheet uses newlines inside cell values
DBEDT_COL_KEYS = ["State", "Honolulu", "Hawaii", "Kauai", "Maui"]  # columns 1–5 in E-8

# Census ACS — contract rent (B25058_001E, utilities excluded)
# ACS releases annually each December; update year when new vintage drops.
CENSUS_ACS_YEAR = "2023"
CENSUS_BASE_URL = f"https://api.census.gov/data/{CENSUS_ACS_YEAR}/acs/acs5"
CENSUS_RENT_VAR = "B25058_001E"   # median contract rent (no utilities) — comparable to Zillow ZORI
CENSUS_NAME_MAP = {
    "Honolulu County, Hawaii": "Honolulu",
    "Hawaii County, Hawaii":   "Hawaii",
    "Maui County, Hawaii":     "Maui",
    "Kauai County, Hawaii":    "Kauai",
}

# Redfin region name → countyData key in the HTML file
COUNTY_MAP = {
    "Honolulu County, HI": "Honolulu",
    "Hawaii County, HI":   "Hawaii",
    "Maui County, HI":     "Maui",
    "Kauai County, HI":    "Kauai",
}

# Zillow ZORI RegionName → countyData key
ZORI_COUNTY_MAP = {
    "Honolulu County": "Honolulu",
    "Hawaii County":   "Hawaii",
    "Maui County":     "Maui",
    "Kauai County":    "Kauai",
}

# Redfin property type → which countyData field to update
PROP_TYPE_MAP = {
    "Single Family Residential": "sfhPrice",
    "Condo/Co-op":               "condoPrice",
}

DEFAULT_FILE = Path(__file__).parent / "squarespace-single-file.html"
# ────────────────────────────────────────────────────────────────


def download_tsv(url: str) -> list[dict]:
    """Download a gzipped TSV from Redfin's S3 bucket and return rows as dicts."""
    print(f"  Downloading {url.split('/')[-1]}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        raw = gzip.decompress(resp.read())
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="\t")
    return list(reader)


def extract_hawaii_prices(rows: list[dict], region_col: str, region_values: dict) -> dict:
    """
    Filter rows to Hawaii regions + target property types,
    find the most recent month for each (region, property_type),
    return {countyData_key: {sfhPrice: int, condoPrice: int}}.
    """
    # Filter to Hawaii + relevant property types
    filtered = []
    for row in rows:
        region = row.get(region_col, "").strip('"')
        prop   = row.get("PROPERTY_TYPE", "").strip('"')
        price  = row.get("MEDIAN_SALE_PRICE", "").strip('"')
        period = row.get("PERIOD_BEGIN", "").strip('"')

        if region not in region_values or prop not in PROP_TYPE_MAP:
            continue
        if not price or not period:
            continue

        filtered.append({
            "key":    region_values[region],
            "field":  PROP_TYPE_MAP[prop],
            "price":  int(float(price)),
            "period": period,
        })

    # Keep only the most recent period per (key, field)
    latest = {}
    for row in filtered:
        k = (row["key"], row["field"])
        if k not in latest or row["period"] > latest[k]["period"]:
            latest[k] = row

    # Restructure as {key: {sfhPrice: X, condoPrice: Y, period: ...}}
    result = {}
    for (key, field), row in latest.items():
        if key not in result:
            result[key] = {"period": row["period"]}
        result[key][field] = row["price"]
        # Keep the most recent period across both property types
        if row["period"] > result[key]["period"]:
            result[key]["period"] = row["period"]

    return result


def fetch_zori_asking_rents() -> dict:
    """
    Download Zillow ZORI county CSV and extract the most recent asking rent
    for each Hawaii county. Returns {countyData_key: askRent_int}.
    State-level askRent is derived as a population-weighted average
    (Honolulu ~72%, Hawaii ~14%, Maui ~10%, Kauai ~4%).
    """
    print(f"  Downloading {ZORI_URL.split('/')[-1]}...")
    req = urllib.request.Request(ZORI_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode("utf-8")

    reader = csv.reader(io.StringIO(raw))
    headers = next(reader)

    result = {}
    for row in reader:
        if len(row) < 10:
            continue
        region_name = row[2]
        state       = row[5]
        if state != "HI" or region_name not in ZORI_COUNTY_MAP:
            continue

        # Find the last non-empty value (most recent month)
        last_val = next(
            (row[i] for i in range(len(row) - 1, 8, -1) if row[i].strip()),
            None,
        )
        if last_val is None:
            continue

        key = ZORI_COUNTY_MAP[region_name]
        result[key] = round(float(last_val))

    # Compute statewide weighted average if all counties present
    weights = {"Honolulu": 0.72, "Hawaii": 0.14, "Maui": 0.10, "Kauai": 0.04}
    if all(k in result for k in weights):
        state_avg = sum(result[k] * w for k, w in weights.items())
        result["State"] = round(state_avg)

    return result


def fetch_census_rent() -> dict:
    """
    Download ACS 5-year median contract rent (B25058_001E) for Hawaii state + 4 counties.
    Contract rent excludes utilities — directly comparable to Zillow ZORI asking rents.
    No API key required (anonymous access is rate-limited but sufficient for monthly runs).
    Returns {countyKey: {rent: int}} plus '_year' metadata key.
    """
    import json

    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    state_url  = f"{CENSUS_BASE_URL}?get={CENSUS_RENT_VAR}&for=state:15"
    county_url = f"{CENSUS_BASE_URL}?get={CENSUS_RENT_VAR},NAME&for=county:*&in=state:15"

    print(f"  Fetching Census ACS {CENSUS_ACS_YEAR} contract rent (B25058_001E)...")
    state_data  = _get(state_url)
    county_data = _get(county_url)

    result = {"_year": CENSUS_ACS_YEAR}

    # State row: [header, data_row]
    s_hdr, s_row = state_data[0], state_data[1]
    result["State"] = {"rent": int(s_row[s_hdr.index(CENSUS_RENT_VAR)])}

    # County rows
    c_hdr, *c_rows = county_data
    rent_idx = c_hdr.index(CENSUS_RENT_VAR)
    name_idx = c_hdr.index("NAME")
    for row in c_rows:
        key = CENSUS_NAME_MAP.get(row[name_idx])
        if key:
            result[key] = {"rent": int(row[rent_idx])}

    return result


def fetch_dbedt_construction() -> dict:
    """
    Download DBEDT QSER construction XLSX and extract E-8:
    'Estimated Value of Private Building Construction Authorizations, By County'
    (in thousands of dollars, quarterly).

    Returns {countyKey: buildAuth_millions} for the most recent complete year,
    plus a '_period' key with the year string.
    Requires openpyxl (pip install openpyxl).
    """
    if not _OPENPYXL_AVAILABLE:
        raise ImportError("openpyxl is required for DBEDT fetch — run: pip install openpyxl")

    print(f"  Downloading E-construction-tables.xlsx...")
    req = urllib.request.Request(DBEDT_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb["E-8"]

    rows = list(ws.iter_rows(values_only=True))

    # Locate the header row — it contains "State" in column 1
    header_idx = None
    for i, row in enumerate(rows):
        if row and len(row) > 1 and row[1] is not None and str(row[1]).strip() == "State":
            header_idx = i
            break

    if header_idx is None:
        raise ValueError("Could not find header row in E-8 worksheet")

    # Data columns: 1=State, 2=Honolulu, 3=Hawaii County, 4=Kauai County, 5=Maui County
    # (indices align with DBEDT_COL_KEYS order)
    data_col_indices = [1, 2, 3, 4, 5]

    # Collect annual rows (skip quarterly "Qtr." rows and float-valued % change rows)
    annual_data = {}
    for row in rows[header_idx + 2:]:   # +2 skips header + "In Thousands" label
        if not row or row[0] is None:
            continue
        year_cell = str(row[0]).strip()

        # Skip quarterly rows
        if "Qtr" in year_cell or "qtr" in year_cell:
            continue
        # Skip percentage-change section (first cell is a float)
        if isinstance(row[0], float):
            continue

        # Clean year string: strip "1/  ", "2/  " footnote prefixes
        year_clean = re.sub(r"^\d+/\s*", "", year_cell).strip()
        try:
            year = int(float(year_clean))
        except (ValueError, TypeError):
            continue

        row_vals = {}
        for j, key in zip(data_col_indices, DBEDT_COL_KEYS):
            if j < len(row) and isinstance(row[j], (int, float)):
                row_vals[key] = row[j]  # thousands of dollars

        if row_vals:
            annual_data[year] = row_vals

    if not annual_data:
        raise ValueError("No annual data parsed from E-8 — check sheet structure")

    latest_year = max(annual_data.keys())
    latest = annual_data[latest_year]

    result = {"_period": str(latest_year)}
    for key, val_thousands in latest.items():
        result[key] = round(val_thousands / 1000)  # → millions, rounded

    return result


def patch_html(html: str, prices: dict) -> str:
    """
    Replace sfhPrice, condoPrice, and askRent values in the countyData object.
    Matches patterns like:
        Honolulu: { income:104264, sfhPrice:1092400, condoPrice:560000, ...
    """
    for county_key, vals in prices.items():
        for field in ("sfhPrice", "condoPrice", "askRent"):
            if field not in vals:
                continue
            pattern = rf'({re.escape(county_key)}:\s*\{{[^}}]*?){field}:\s*\d+'
            replacement = rf'\g<1>{field}:{vals[field]}'
            new_html = re.sub(pattern, replacement, html)
            if new_html == html:
                print(f"  WARNING: could not find {county_key}.{field} in HTML")
            html = new_html

    return html


def main():
    dry_run = "--dry-run" in sys.argv
    file_idx = sys.argv.index("--file") + 1 if "--file" in sys.argv else None
    target = Path(sys.argv[file_idx]) if file_idx else DEFAULT_FILE

    if not target.exists():
        print(f"ERROR: {target} not found")
        sys.exit(1)

    print("Fetching Redfin housing market data...")

    # Download state-level data (for "State" key = Hawaii statewide)
    state_rows = download_tsv(STATE_URL)
    state_prices = extract_hawaii_prices(
        state_rows,
        region_col="STATE_CODE",
        region_values={"HI": "State"},
    )

    # Download county-level data
    county_rows = download_tsv(COUNTY_URL)
    county_prices = extract_hawaii_prices(
        county_rows,
        region_col="REGION",
        region_values=COUNTY_MAP,
    )

    # Merge state + county sale prices
    all_prices = {**state_prices, **county_prices}

    if not all_prices:
        print("ERROR: No Hawaii data found in Redfin exports")
        sys.exit(1)

    # Fetch Census ACS contract rent (existing leases, no utilities) and merge in
    print("\nFetching Census ACS contract rent (existing leases)...")
    try:
        census_rents = fetch_census_rent()
        acs_year = census_rents.pop("_year", CENSUS_ACS_YEAR)
        for key, vals in census_rents.items():
            if key not in all_prices:
                all_prices[key] = {}
            all_prices[key].update(vals)
        print(f"  Got contract rent (ACS {acs_year}) for: {', '.join(census_rents.keys())}")
    except Exception as e:
        print(f"  WARNING: Census rent fetch failed ({e}) — rent will not be updated")

    # Fetch Zillow ZORI asking rents and merge in
    print("\nFetching Zillow ZORI asking rent data...")
    try:
        zori_rents = fetch_zori_asking_rents()
        for key, ask_rent in zori_rents.items():
            if key not in all_prices:
                all_prices[key] = {}
            all_prices[key]["askRent"] = ask_rent
        print(f"  Got askRent for: {', '.join(zori_rents.keys())}")
    except Exception as e:
        print(f"  WARNING: Zillow ZORI fetch failed ({e}) — askRent will not be updated")

    # Fetch DBEDT construction authorization data and merge in
    print("\nFetching DBEDT construction authorization data...")
    try:
        dbedt_data = fetch_dbedt_construction()
        build_period = dbedt_data.pop("_period", "?")
        for key, build_auth in dbedt_data.items():
            if key not in all_prices:
                all_prices[key] = {}
            all_prices[key]["buildAuth"] = build_auth
        print(f"  Got buildAuth ({build_period}) for: {', '.join(dbedt_data.keys())}")
    except Exception as e:
        build_period = "?"
        print(f"  WARNING: DBEDT construction fetch failed ({e}) — buildAuth will not be updated")

    # Print summary
    print("\nLatest data:\n")
    print(f"  {'County':<12} {'SFH':>12} {'Condo':>12} {'ContractRent':>13} {'AskRent':>10} {'BuildAuth($M)':>14}  {'Period'}")
    print(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*13} {'─'*10} {'─'*14}  {'─'*10}")
    for key in ["State", "Honolulu", "Maui", "Hawaii", "Kauai"]:
        if key not in all_prices:
            continue
        v = all_prices[key]
        sfh       = f"${v.get('sfhPrice', 0):>10,}" if "sfhPrice" in v else f"{'N/A':>11}"
        condo     = f"${v.get('condoPrice', 0):>10,}" if "condoPrice" in v else f"{'N/A':>11}"
        crent     = f"${v.get('rent', 0):>11,}" if "rent" in v else f"{'N/A':>12}"
        askrent   = f"${v.get('askRent', 0):>8,}" if "askRent" in v else f"{'N/A':>9}"
        buildauth = f"${v.get('buildAuth', 0):>11,}M" if "buildAuth" in v else f"{'N/A':>13}"
        print(f"  {key:<12} {sfh} {condo} {crent} {askrent} {buildauth}  {v.get('period', build_period)}")

    if dry_run:
        print("\n--dry-run: no files modified")
        return

    # Read, patch, write
    html = target.read_text(encoding="utf-8")
    patched = patch_html(html, all_prices)

    if patched == html:
        print("\nNo changes needed — prices already match Redfin data.")
        return

    target.write_text(patched, encoding="utf-8")
    print(f"\nUpdated {target.name} with latest Redfin prices.")


if __name__ == "__main__":
    main()
