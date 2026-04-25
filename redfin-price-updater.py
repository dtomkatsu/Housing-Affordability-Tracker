#!/usr/bin/env python3
"""
Redfin + Zillow + DBEDT → Housing Affordability Tracker price updater.

Downloads:
  - Redfin public S3 TSV files → median sale prices (SFH + condo) per county
  - Zillow ZORI county CSV     → median asking rent per county
  - DBEDT QSER construction XLSX → private building authorization values per county
  - HHFDC county income schedule PDFs → HUD median family income per county
  - HUD State Income Limits PDF  → statewide median family income

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
from pathlib import Path

# Shared HTTP helper — adds timeout + retry to all fetches
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.http_client import fetch_bytes, fetch_text   # noqa: E402
from common.html_patcher import patch_html_files  # noqa: E402

try:
    import openpyxl
    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False

try:
    import pdfplumber
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False

# ─── CONFIG ─────────────────────────────────────────────────────
STATE_URL  = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/state_market_tracker.tsv000.gz"
COUNTY_URL = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/county_market_tracker.tsv000.gz"
ZORI_URL   = "https://files.zillowstatic.com/research/public_csvs/zori/County_zori_uc_sfrcondomfr_sm_month.csv"
DBEDT_URL  = "https://files.hawaii.gov/dbedt/economic/data_reports/qser/E-construction-tables.xlsx"

# HHFDC county income schedule PDFs (HUD income limits, published by state of Hawaii).
# The "MEDIAN" column in each schedule is HUD's FY2025 4-person median family income.
HHFDC_PDF_TEMPLATE = "https://dbedt.hawaii.gov/hhfdc/files/2025/05/{county}-County-2025.pdf"
HHFDC_COUNTIES     = ["Honolulu", "Hawaii", "Maui", "Kauai"]

# HUD State Income Limits report (FY2025) — contains each state's MFI including HI.
HUD_STATE_IL_URL   = "https://www.huduser.gov/portal/datasets/il/il25/State-Incomelimits-Report-FY25.pdf"
HUD_FY             = "FY 2025"

# DBEDT E-8 column header → countyData key (columns in order: State, Honolulu, Hawaii, Kauai, Maui)
# The header row in the sheet uses newlines inside cell values
DBEDT_COL_KEYS = ["State", "Honolulu", "Hawaii", "Kauai", "Maui"]  # columns 1–5 in E-8

# ─── Rent anchor year (SINGLE SOURCE OF TRUTH) ──────────────────
# Both the ACS contract-rent dollar anchor and the BLS rent-CPI base-year
# average must align on the same vintage — otherwise the scaling factor
# "BLS(now) / BLS(anchor_year_avg)" applied to "ACS(anchor_year) dollars"
# produces a dollar value that is anchored to a different year than the
# index says. Keep both pointing at the same YEAR constant.
#
# RE-ANCHORING CADENCE (see METHODOLOGY.md): bump this every December
# when a new ACS 5-year vintage is released. Pull the fresh Honolulu
# contract rent directly from the Census API response — no more
# hardcoded dollar values.
RENT_ANCHOR_YEAR = "2024"

# Census ACS — contract rent (B25058_001E, utilities excluded)
CENSUS_ACS_YEAR = RENT_ANCHOR_YEAR
CENSUS_BASE_URL = f"https://api.census.gov/data/{CENSUS_ACS_YEAR}/acs/acs5"
CENSUS_RENT_VAR = "B25058_001E"   # median contract rent (no utilities) — comparable to Zillow ZORI
CENSUS_NAME_MAP = {
    "Honolulu County, Hawaii": "Honolulu",
    "Hawaii County, Hawaii":   "Hawaii",
    "Maui County, Hawaii":     "Maui",
    "Kauai County, Hawaii":    "Kauai",
}

# BLS CPI: Honolulu MSA — "Rent of primary residence" (existing tenants, not new leases)
# Series CUURS49ASEHA, not seasonally adjusted, base 1982-84=100.
# We scale the live ACS Honolulu contract rent (fetched each run) by the
# BLS index ratio (latest / anchor-year avg) to get a monthly-current estimate.
BLS_API_URL     = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_RENT_SERIES = "CUURS49ASEHA"
BLS_BASE_YEAR   = RENT_ANCHOR_YEAR

# NTR/ATR national benchmarks — manually refreshed quarterly from BLS research
# series R-CPI-NTR and R-CPI-ATR. Used only for a dev-facing sanity check on
# the Honolulu rent nowcast (see audit_rent_nowcast_vs_ntr below). Not read by
# the dashboard UI. See METHODOLOGY.md § Quarterly NTR/ATR benchmark refresh.
NTR_ATR_BENCHMARKS_PATH = Path(__file__).parent / "data" / "ntr_atr_benchmarks.json"

# Blended rent nowcast — weights for combining the lagging BLS rent CPI with
# Zillow ZORI's leading asking-rent signal. Both series are expressed as
# growth factors vs. RENT_ANCHOR_YEAR and applied to the same ACS dollar
# anchor:
#   blended_rent = acs_anchor × (CPI_WEIGHT · bls_ratio + (1−CPI_WEIGHT) · zori_ratio)
# Rationale: the BLS Honolulu rent index (CUURS49ASEHA) lags market rent by
# ~12 months due to (a) 6-month sampling with even attribution and (b) heavy
# weight on continuing tenants whose rents reset slowly on renewal. ZORI is
# asking-only and over-reacts to turnover. A 70/30 weight captures most of
# the lag without letting asking-rent swings dominate the reported number.
# See Cleveland Fed WP 22-38r (new-tenant rent leads CPI by ~1 year).
BLENDED_RENT_CPI_WEIGHT = 0.7

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

DEFAULT_FILES = [
    Path(__file__).parent / "squarespace-single-file.html",
    Path(__file__).parent / "index.html",
]
# ────────────────────────────────────────────────────────────────


def download_tsv(url: str) -> list[dict]:
    """Download a gzipped TSV from Redfin's S3 bucket and return rows as dicts."""
    print(f"  Downloading {url.split('/')[-1]}...")
    raw = gzip.decompress(fetch_bytes(url))
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
    Download Zillow ZORI county CSV and extract:
      - The most recent asking rent for each Hawaii county (→ result[key])
      - The RENT_ANCHOR_YEAR annual average per county, used as a common
        anchor with BLS rent CPI for the blended nowcast
        (→ result["_anchor_avg"][key])

    Returns {countyData_key: askRent_int, "_period": "YYYY-MM",
             "_anchor_avg": {...}, "_anchor_year": "YYYY", "_yoy_pct": {...}}.
    State-level askRent is derived as a population-weighted average
    (Honolulu ~72%, Hawaii ~14%, Maui ~10%, Kauai ~4%).

    The anchor-year average MUST track RENT_ANCHOR_YEAR — the BLS rent CPI
    ratio is computed against the same year, and the blended nowcast assumes
    both series share the anchor. A drifted anchor here would silently shift
    the ZORI growth factor relative to BLS.
    """
    print(f"  Downloading {ZORI_URL.split('/')[-1]}...")
    raw = fetch_text(ZORI_URL)

    reader = csv.reader(io.StringIO(raw))
    headers = next(reader)

    # Pre-compute which column indices belong to RENT_ANCHOR_YEAR for the
    # anchor-avg calc. ZORI column headers are ISO dates like "2024-01-31".
    anchor_prefix = f"{RENT_ANCHOR_YEAR}-"
    cols_anchor = [i for i, h in enumerate(headers) if h.startswith(anchor_prefix)]

    result = {}
    anchor_avg = {}
    yoy_pct = {}   # per-county YoY % using same-month-prior-year column
    latest_date_header = None  # e.g. "2026-03-31" → we'll convert to "2026-03"
    for row in reader:
        if len(row) < 10:
            continue
        region_name = row[2]
        state       = row[5]
        if state != "HI" or region_name not in ZORI_COUNTY_MAP:
            continue

        # Find the last non-empty column (most recent month) — return both value and header
        last_idx = next(
            (i for i in range(len(row) - 1, 8, -1) if row[i].strip()),
            None,
        )
        if last_idx is None:
            continue

        key = ZORI_COUNTY_MAP[region_name]
        result[key] = round(float(row[last_idx]))
        # Capture the column header (date) once; should be identical across counties
        if latest_date_header is None and last_idx < len(headers):
            latest_date_header = headers[last_idx]

        # Anchor-year annual average — skip empty cells / unparseable values
        vals_anchor = []
        for i in cols_anchor:
            if i < len(row) and row[i].strip():
                try:
                    vals_anchor.append(float(row[i]))
                except ValueError:
                    pass
        if vals_anchor:
            anchor_avg[key] = sum(vals_anchor) / len(vals_anchor)

        # YoY: current column vs the column 12 months earlier. ZORI publishes
        # every month so the same position back by 12 is the same calendar month
        # a year ago. Used by the NTR/ATR sanity-check audit.
        if last_idx >= 12 + 9:  # +9 is the first data column (after metadata)
            prior_cell = row[last_idx - 12].strip() if last_idx - 12 < len(row) else ""
            if prior_cell:
                try:
                    prior_val = float(prior_cell)
                    if prior_val > 0:
                        yoy_pct[key] = (float(row[last_idx]) / prior_val - 1.0) * 100.0
                except ValueError:
                    pass

    # Compute statewide weighted average if all counties present
    weights = {"Honolulu": 0.72, "Hawaii": 0.14, "Maui": 0.10, "Kauai": 0.04}
    if all(k in result for k in weights):
        state_avg = sum(result[k] * w for k, w in weights.items())
        result["State"] = round(state_avg)
    # For the anchor-year average, allow partial coverage (Zillow started
    # publishing some small-market counties like Kauai only recently, so Kauai
    # can be missing from the anchor year even when its current value is
    # reported). When that happens, re-normalize the weights across the
    # counties we actually have.
    present_anchor = {k: weights[k] for k in weights if k in anchor_avg}
    if len(present_anchor) >= 2:
        wsum = sum(present_anchor.values())
        anchor_avg["State"] = sum(
            anchor_avg[k] * (w / wsum) for k, w in present_anchor.items()
        )

    # Convert "2026-03-31" → "2026-03" for consistency with other period fields
    if latest_date_header and len(latest_date_header) >= 7:
        result["_period"] = latest_date_header[:7]

    result["_anchor_avg"] = anchor_avg
    result["_anchor_year"] = RENT_ANCHOR_YEAR
    result["_yoy_pct"] = yoy_pct  # per-county YoY % — used by NTR/ATR audit
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
        return json.loads(fetch_bytes(url))

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


def fetch_bls_rent_ratio() -> tuple[float, str, float | None]:
    """
    Fetch BLS CPI series CUURS49ASEHA (Honolulu MSA, rent of primary residence)
    and return (ratio, period, yoy_pct).

    ratio   — current_idx / RENT_ANCHOR_YEAR average (used to scale ACS dollars)
    period  — ISO "YYYY-MM" of the latest observation
    yoy_pct — 12-month YoY % change for the same month one year prior, or None
              if the 12-month-prior observation is not available (e.g. anchor
              year itself). Used by the NTR/ATR sanity-check audit.

    Raises on network/parse failure so callers can decide whether to fall back
    to raw ACS values.
    """
    import json
    import datetime

    current_year = str(datetime.date.today().year)
    payload = json.dumps({
        "seriesid": [BLS_RENT_SERIES],
        "startyear": BLS_BASE_YEAR,
        "endyear": current_year,
    }).encode()
    data = json.loads(
        fetch_bytes(BLS_API_URL, data=payload, headers={"Content-Type": "application/json"})
    )

    series_data = data["Results"]["series"][0]["data"]

    # Base-year annual average (exclude M13 annual row, skip "-" missing)
    base_vals = [
        float(r["value"])
        for r in series_data
        if r["year"] == BLS_BASE_YEAR
        and r["period"].startswith("M")
        and r["period"] != "M13"
        and r["value"] != "-"
    ]
    if not base_vals:
        raise ValueError(f"No BLS monthly data found for base year {BLS_BASE_YEAR}")
    base_avg = sum(base_vals) / len(base_vals)

    # Most recent monthly value (BLS returns newest-first; skip M13 annual and missing)
    recent = next(
        (
            r for r in series_data
            if r["period"].startswith("M")
            and r["period"] != "M13"
            and r["value"] != "-"
        ),
        None,
    )
    if not recent:
        raise ValueError("No recent BLS monthly value found")

    current_idx = float(recent["value"])
    ratio       = current_idx / base_avg
    period      = f"{recent['year']}-{recent['period'][1:].zfill(2)}"  # e.g. "2026-03"

    # YoY vs. same month a year ago (skip if prior-year observation missing —
    # common when the "current year" is also the anchor year).
    prior_year = str(int(recent["year"]) - 1)
    prior = next(
        (
            r for r in series_data
            if r["year"] == prior_year
            and r["period"] == recent["period"]
            and r["value"] != "-"
        ),
        None,
    )
    yoy_pct: float | None = None
    if prior is not None:
        prior_val = float(prior["value"])
        if prior_val > 0:
            yoy_pct = (current_idx / prior_val - 1.0) * 100.0

    yoy_str = f", YoY {yoy_pct:+.2f}%" if yoy_pct is not None else ""
    print(f"  BLS {BLS_RENT_SERIES}: base_avg={base_avg:.2f}, current={current_idx:.3f}, "
          f"ratio={ratio:.4f} (period {period}{yoy_str})")
    return ratio, period, yoy_pct


def fetch_bls_rent(honolulu_acs_anchor: int) -> dict:
    """
    Scales the live ACS Honolulu contract rent (from the current run's
    fetch_census_rent()) by the BLS CPI index ratio to produce a
    monthly-current estimate for existing-tenant rent in Honolulu.
    Neighbor islands are scaled in main() using the ratio directly
    (see fetch_bls_rent_ratio).

    *honolulu_acs_anchor* must be the ACS {RENT_ANCHOR_YEAR} 5-year
    Honolulu contract rent (dollars). Passing the wrong vintage here
    double-scales the index and produces a silently wrong dollar value.

    Returns {"Honolulu": {"rent": int}, "_period": "YYYY-MM",
             "_ratio": float, "_yoy_pct": float|None}.
    """
    ratio, period, yoy_pct = fetch_bls_rent_ratio()
    scaled_rent   = round(honolulu_acs_anchor * ratio)
    print(f"  → Honolulu rent ${scaled_rent:,} "
          f"(anchor ACS {RENT_ANCHOR_YEAR} ${honolulu_acs_anchor:,} × ratio {ratio:.4f}, "
          f"BLS period {period})")
    return {
        "Honolulu": {"rent": scaled_rent},
        "_period":  period,
        "_ratio":   ratio,
        "_yoy_pct": yoy_pct,
    }


def blend_rent_nowcast(
    acs_anchor: float,
    bls_ratio: float,
    zori_ratio: float,
    cpi_weight: float = BLENDED_RENT_CPI_WEIGHT,
) -> dict:
    """
    Blend BLS-CPI-scaled rent (lagging ~12 mo) with ZORI-implied rent (leading)
    to nowcast current tenant rent. Both components use the same ACS dollar
    anchor (RENT_ANCHOR_YEAR); only the anchor→present growth factor differs.

      bls_ratio    = CUURS49ASEHA(latest) / CUURS49ASEHA(anchor annual avg)
      zori_ratio   = ZORI(latest) / ZORI(anchor annual avg)  [per county; state
                     ratio used as proxy when a county's anchor avg is missing]
      blended_rent = acs_anchor × ( w·bls_ratio + (1−w)·zori_ratio )

    Returns a dict with the blended value plus the two single-source components
    so callers can log all three (useful for audit and the methodology tooltip).
    """
    w = cpi_weight
    blended_factor = w * bls_ratio + (1 - w) * zori_ratio
    return {
        "blended":      round(acs_anchor * blended_factor),
        "cpi_scaled":   round(acs_anchor * bls_ratio),
        "zori_implied": round(acs_anchor * zori_ratio),
        "bls_ratio":    bls_ratio,
        "zori_ratio":   zori_ratio,
        "cpi_weight":   w,
    }


def _load_ntr_atr_benchmarks() -> dict:
    """Load the manually-refreshed national NTR/ATR YoY benchmark file.

    Missing file or missing values → returns an empty-ish dict with `ntr_yoy_pct`
    and `atr_yoy_pct` both None. Callers degrade gracefully to a reduced audit.
    """
    import json
    empty = {"ntr_yoy_pct": None, "atr_yoy_pct": None,
             "latest_quarter": None, "last_refreshed": None,
             "sanity_band_pp": 8.0}
    if not NTR_ATR_BENCHMARKS_PATH.exists():
        return empty
    try:
        d = json.loads(NTR_ATR_BENCHMARKS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return empty
    return {
        "ntr_yoy_pct":    d.get("ntr_yoy_pct"),
        "atr_yoy_pct":    d.get("atr_yoy_pct"),
        "latest_quarter": d.get("latest_quarter"),
        "last_refreshed": d.get("last_refreshed"),
        "sanity_band_pp": float(d.get("_sanity_band_pp") or 8.0),
    }


def audit_rent_nowcast_vs_ntr(
    hnl_cpi_yoy:  float | None,
    hnl_zori_yoy: float | None,
    cpi_weight:   float = BLENDED_RENT_CPI_WEIGHT,
) -> None:
    """Print a dev-facing sanity check: Honolulu rent signals vs national NTR/ATR.

    No HTML surface, no return value. Run after the blended nowcast so we can
    log what percentage change each component (existing tenant, asking, blended)
    is showing and compare it to the BLS national research series. A divergence
    greater than *sanity_band_pp* prints a WARNING — it doesn't block the run.

    BLS R-CPI-NTR/R-CPI-ATR are national only (no Hawaii cut exists), so this
    is a directional check, not an equality test. We expect Hawaii to run
    hotter than national during tight-supply periods and cooler when the
    mainland surges (post-2021).
    """
    bm = _load_ntr_atr_benchmarks()
    ntr, atr = bm["ntr_yoy_pct"], bm["atr_yoy_pct"]

    # Honolulu blended YoY: since blend is linear in the two ratios and both
    # ratios share the same ACS anchor, the blended 12-month % change equals
    # w·CPI_YoY + (1-w)·ZORI_YoY (first-order Taylor; ratios near 1 → the
    # approximation is within ~0.1 pp of the exact blended ratio-of-ratios).
    hnl_blended_yoy = None
    if hnl_cpi_yoy is not None and hnl_zori_yoy is not None:
        hnl_blended_yoy = cpi_weight * hnl_cpi_yoy + (1 - cpi_weight) * hnl_zori_yoy

    band = bm["sanity_band_pp"]
    q    = bm["latest_quarter"] or "— benchmark not yet refreshed —"

    def _fmt(v):
        return f"{v:+.2f}%" if v is not None else "   n/a  "

    print(f"\nRent sanity check — Honolulu vs national NTR/ATR (BLS {q})")
    print(f"  Honolulu BLS rent CPI  YoY: {_fmt(hnl_cpi_yoy)}   "
          f"← existing tenants ({BLS_RENT_SERIES})")
    print(f"  Honolulu ZORI asking   YoY: {_fmt(hnl_zori_yoy)}   "
          f"← new listings (Zillow ZORI)")
    print(f"  National R-CPI-NTR     YoY: {_fmt(ntr)}   "
          f"← new tenants (national research series)")
    print(f"  National R-CPI-ATR     YoY: {_fmt(atr)}   "
          f"← all tenants (national research series)")
    print(f"  Honolulu blended       YoY: {_fmt(hnl_blended_yoy)}   "
          f"← {int(cpi_weight*100)}·CPI + {int((1-cpi_weight)*100)}·ZORI")

    # Warn when Honolulu blended diverges sharply from national NTR — the closest
    # national analog to our nowcast (both emphasize new-tenant momentum). If no
    # benchmark, skip with a hint so the user knows why.
    if ntr is None:
        print(f"  (no NTR benchmark on file — refresh {NTR_ATR_BENCHMARKS_PATH.name} "
              f"from https://www.bls.gov/cpi/research-series/r-cpi-ntr.htm)")
        return
    if hnl_blended_yoy is None:
        return
    gap = hnl_blended_yoy - ntr
    if abs(gap) > band:
        print(f"  ⚠ WARNING: Honolulu blended YoY differs from national NTR by "
              f"{gap:+.2f} pp (band ±{band:.1f} pp). Investigate before publishing.")


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
    raw = fetch_bytes(DBEDT_URL)

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


def fetch_hhfdc_county_mfi() -> dict:
    """
    Download HHFDC county income schedule PDFs and extract HUD median family
    income (4-person) for each county. The MFI appears as the first dollar
    figure on page 1 (e.g. "$129,300" for Honolulu).

    Returns {countyKey: {"income": int}}, plus a "_period" key.
    Requires pdfplumber (pip install pdfplumber).
    """
    if not _PDFPLUMBER_AVAILABLE:
        raise ImportError("pdfplumber is required for HHFDC fetch — run: pip install pdfplumber")

    result = {"_period": HUD_FY}
    for county in HHFDC_COUNTIES:
        url = HHFDC_PDF_TEMPLATE.format(county=county)
        print(f"  Downloading {county}-County-2025.pdf...")
        raw = fetch_bytes(url)

        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = pdf.pages[0].extract_text() or ""

        m = re.search(r"\$(\d{2,3}(?:,\d{3})+)", text)
        if not m:
            print(f"  WARNING: could not parse MFI from {county} PDF")
            continue
        result[county] = {"income": int(m.group(1).replace(",", ""))}

    return result


def fetch_hud_state_mfi() -> dict:
    """
    Download HUD's FY 2025 State Income Limits report PDF and extract the
    Hawaii statewide median family income. The Hawaii row appears as:
        HAWAII
        FY 2025 MFI: 123000 30% OF MEDIAN ...

    Returns {"State": {"income": int}, "_period": HUD_FY}.
    Requires pdfplumber.
    """
    if not _PDFPLUMBER_AVAILABLE:
        raise ImportError("pdfplumber is required for HUD state fetch — run: pip install pdfplumber")

    print(f"  Downloading State-Incomelimits-Report-FY25.pdf...")
    raw = fetch_bytes(HUD_STATE_IL_URL)

    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        # Search all pages for Hawaii — alphabetically it's on page 1, but don't hardcode.
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    m = re.search(r"HAWAII\s+FY\s*2025\s*MFI:\s*(\d+)", text)
    if not m:
        raise ValueError("Could not parse Hawaii MFI from HUD state PDF")

    return {"_period": HUD_FY, "State": {"income": int(m.group(1))}}


ZORI_PERIOD_RE = re.compile(
    r"/\* ZORI_PERIOD_START \*/.*?/\* ZORI_PERIOD_END \*/",
    flags=re.DOTALL,
)
BLS_RENT_PERIOD_RE = re.compile(
    r"/\* BLS_RENT_PERIOD_START \*/.*?/\* BLS_RENT_PERIOD_END \*/",
    flags=re.DOTALL,
)
HOUSING_PERIOD_RE = re.compile(
    r"/\* HOUSING_PERIOD_START \*/.*?/\* HOUSING_PERIOD_END \*/",
    flags=re.DOTALL,
)


def patch_periods(html: str, zori_period: str | None, bls_rent_period: str | None,
                  housing_period: str | None = None) -> str:
    """Patch the ZORI_PERIOD, BLS_RENT_PERIOD, and HOUSING_PERIOD marker
    blocks if present. Missing markers are silently skipped."""
    if zori_period and ZORI_PERIOD_RE.search(html):
        block = (
            "/* ZORI_PERIOD_START */\n"
            f'const zoriLatestPeriod = "{zori_period}";\n'
            "/* ZORI_PERIOD_END */"
        )
        html = ZORI_PERIOD_RE.sub(lambda m: block, html, count=1)
    if bls_rent_period and BLS_RENT_PERIOD_RE.search(html):
        block = (
            "/* BLS_RENT_PERIOD_START */\n"
            f'const blsRentLatestPeriod = "{bls_rent_period}";\n'
            "/* BLS_RENT_PERIOD_END */"
        )
        html = BLS_RENT_PERIOD_RE.sub(lambda m: block, html, count=1)
    if housing_period and HOUSING_PERIOD_RE.search(html):
        block = (
            "/* HOUSING_PERIOD_START */\n"
            f'const housingLatestPeriod = "{housing_period}";\n'
            "/* HOUSING_PERIOD_END */"
        )
        html = HOUSING_PERIOD_RE.sub(lambda m: block, html, count=1)
    return html


def patch_html(html: str, prices: dict) -> str:
    """
    Replace sfhPrice, condoPrice, rent, askRent, and income values in
    the countyData object by simple find-and-replace on the lines.
    """
    for county_key, vals in prices.items():
        # Find the line with this county
        pattern = rf'^(\s*{re.escape(county_key)}:\s*{{[^}}]*)'

        def replacer(match):
            line_text = match.group(1)
            # Replace each field value in this line
            for field in ("sfhPrice", "condoPrice", "rent", "askRent", "income"):
                if field in vals:
                    # Find old value and replace with new
                    line_text = re.sub(
                        rf'{field}:\d+',
                        f'{field}:{vals[field]}',
                        line_text
                    )
            return line_text

        new_html = re.sub(pattern, replacer, html, flags=re.MULTILINE)

        # Check if anything changed by testing each field individually
        for field in ("sfhPrice", "condoPrice", "rent", "askRent", "income"):
            if field in vals and f'{field}:{vals[field]}' not in new_html:
                print(f"  WARNING: could not set {county_key}.{field}")

        html = new_html

    return html


def _fetch_sale_prices() -> dict:
    """Download Redfin state + county TSVs and return merged price dict.

    Returns {countyKey: {sfhPrice, condoPrice, period, ...}} for all
    Hawaii counties plus "State".  Exits the process on total failure
    (no Hawaii data at all is unrecoverable).
    """
    print("Fetching Redfin housing market data...")
    state_rows  = download_tsv(STATE_URL)
    county_rows = download_tsv(COUNTY_URL)
    prices = {
        **extract_hawaii_prices(state_rows,  region_col="STATE_CODE", region_values={"HI": "State"}),
        **extract_hawaii_prices(county_rows, region_col="REGION",     region_values=COUNTY_MAP),
    }
    if not prices:
        print("ERROR: No Hawaii data found in Redfin exports")
        sys.exit(1)
    return prices


def _fetch_rents(all_prices: dict) -> tuple[
    float | None,   # bls_ratio
    str   | None,   # bls_rent_period
    float | None,   # bls_rent_yoy
    str   | None,   # zori_period
    dict,           # zori_yoy_map  {countyKey: YoY%}
]:
    """Fetch Census ACS, BLS rent CPI, and Zillow ZORI; merge into *all_prices*.

    Execution order matters:
      1. Census ACS contract rent → sets the RENT_ANCHOR_YEAR dollar anchor
         for all counties.  Must run before BLS scaling so the anchors are
         captured before BLS overwrites the Honolulu value.
      2. BLS rent CPI → scales the ACS anchor to the current month.
      3. ZORI → adds asking-rent and 2024 baseline; drives the blended nowcast.
      4. Blended nowcast → overwrites CPI-only rent with 70/30 composite.

    Returns metadata consumed by downstream callers (period strings for HTML
    patching, YoY floats for the NTR/ATR audit).  All failures are soft-warned;
    the function never raises.
    """
    # ── 1. Census ACS contract rent ──────────────────────────────────────────
    print("\nFetching Census ACS contract rent (existing leases)...")
    try:
        census_rents = fetch_census_rent()
        acs_year = census_rents.pop("_year", CENSUS_ACS_YEAR)
        for key, vals in census_rents.items():
            all_prices.setdefault(key, {}).update(vals)
        print(f"  Got contract rent (ACS {acs_year}) for: {', '.join(census_rents.keys())}")
    except Exception as e:
        print(f"  WARNING: Census rent fetch failed ({e}) — rent will not be updated")

    # Snapshot ACS anchors BEFORE BLS/ZORI scaling overwrites them.
    # Both BLS ratio and ZORI ratio are applied to the same anchor year so the
    # blended formula is dimensionally consistent.
    acs_rent_anchor    = {k: v["rent"] for k, v in all_prices.items() if "rent" in v}
    honolulu_acs_anchor = acs_rent_anchor.get("Honolulu")

    # ── 2. BLS rent CPI ──────────────────────────────────────────────────────
    bls_rent_period = None
    bls_ratio       = None
    bls_rent_yoy    = None
    print("\nFetching BLS CPI rent index (existing tenants, monthly)...")
    try:
        if honolulu_acs_anchor is None:
            raise RuntimeError(
                f"no ACS {RENT_ANCHOR_YEAR} Honolulu anchor — Census fetch must precede BLS scaling"
            )
        bls_rent = fetch_bls_rent(honolulu_acs_anchor)
        bls_rent_period = bls_rent.pop("_period", None)
        bls_ratio       = bls_rent.pop("_ratio",   None)
        bls_rent_yoy    = bls_rent.pop("_yoy_pct", None)
        for key, vals in bls_rent.items():
            all_prices.setdefault(key, {}).update(vals)
        if bls_ratio:
            for key in ("Maui", "Hawaii", "Kauai", "State"):
                if key in all_prices and "rent" in all_prices[key]:
                    anchor = acs_rent_anchor.get(key, all_prices[key]["rent"])
                    all_prices[key]["rent"] = round(anchor * bls_ratio)
                    print(f"  {key}: ACS ${anchor:,} × {bls_ratio:.4f} "
                          f"= ${all_prices[key]['rent']:,} (BLS-scaled)")
        print(f"  Updated rents to BLS-scaled estimate ({bls_rent_period})")
    except Exception as e:
        print(f"  WARNING: BLS rent fetch failed ({e}) — rents stay at raw ACS values")

    # ── 3. Zillow ZORI ───────────────────────────────────────────────────────
    zori_period      = None
    zori_anchor_avg  = {}
    zori_yoy_map     = {}
    print("\nFetching Zillow ZORI asking rent data...")
    try:
        zori_rents       = fetch_zori_asking_rents()
        zori_period      = zori_rents.pop("_period",      None)
        zori_anchor_avg  = zori_rents.pop("_anchor_avg",  {}) or {}
        zori_anchor_year = zori_rents.pop("_anchor_year", RENT_ANCHOR_YEAR)
        zori_yoy_map     = zori_rents.pop("_yoy_pct",     {}) or {}
        for key, ask_rent in zori_rents.items():
            all_prices.setdefault(key, {})["askRent"] = ask_rent
        print(f"  Got askRent ({zori_period or '?'}) for: {', '.join(zori_rents.keys())}")
    except Exception as e:
        print(f"  WARNING: Zillow ZORI fetch failed ({e}) — askRent will not be updated")
        zori_anchor_year = RENT_ANCHOR_YEAR

    # ── 4. Blended rent nowcast ───────────────────────────────────────────────
    # 70% BLS (lagging ~12 mo, existing tenants) + 30% ZORI (leading, new listings).
    # Falls back to CPI-only rent when either input is missing.
    if bls_ratio and zori_anchor_avg:
        zori_ratios: dict[str, float] = {}
        for key in ("Honolulu", "Maui", "Hawaii", "Kauai", "State"):
            cur  = (all_prices.get(key) or {}).get("askRent")
            base = zori_anchor_avg.get(key)
            if cur and base:
                zori_ratios[key] = cur / base
        # Proxy missing-county ZORI ratios from the state-level ratio (same
        # approach as the Honolulu BLS CPI being applied statewide).
        proxy = zori_ratios.get("State")
        if proxy is not None:
            for key in ("Honolulu", "Maui", "Hawaii", "Kauai"):
                if key not in zori_ratios and (all_prices.get(key) or {}).get("askRent"):
                    zori_ratios[key] = proxy
                    print(f"  {key}: no {zori_anchor_year} ZORI baseline — "
                          f"using state ratio {proxy:.4f} as proxy")

        print(f"\nComputing blended rent nowcast "
              f"({int(BLENDED_RENT_CPI_WEIGHT*100)}% CPI / "
              f"{int((1-BLENDED_RENT_CPI_WEIGHT)*100)}% ZORI)...")
        for key in ("Honolulu", "Maui", "Hawaii", "Kauai", "State"):
            v = all_prices.get(key)
            if not v or key not in acs_rent_anchor or key not in zori_ratios:
                continue
            b = blend_rent_nowcast(
                acs_anchor = acs_rent_anchor[key],
                bls_ratio  = bls_ratio,
                zori_ratio = zori_ratios[key],
            )
            v["rent"] = b["blended"]
            print(f"  {key:<9}  CPI-scaled ${b['cpi_scaled']:>5,}  "
                  f"ZORI-implied ${b['zori_implied']:>5,}  "
                  f"→ blended ${b['blended']:>5,}  "
                  f"(bls_ratio={b['bls_ratio']:.3f}, zori_ratio={b['zori_ratio']:.3f})")
    else:
        print("  Skipping blended nowcast (missing BLS ratio or ZORI 2024 baseline)")

    return bls_ratio, bls_rent_period, bls_rent_yoy, zori_period, zori_yoy_map


def _fetch_income_and_construction(all_prices: dict) -> str:
    """Fetch HHFDC county MFI, HUD state MFI, and DBEDT build auth; merge into *all_prices*.

    Returns the DBEDT period string (e.g. "2025") for use in the summary table.
    All three fetches are soft-warned on failure — the function never raises.
    """
    print("\nFetching HHFDC county median family incomes (HUD FY 2025)...")
    try:
        hhfdc_incomes = fetch_hhfdc_county_mfi()
        hhfdc_incomes.pop("_period", None)
        for key, vals in hhfdc_incomes.items():
            all_prices.setdefault(key, {}).update(vals)
        print(f"  Got income for: {', '.join(hhfdc_incomes.keys())}")
    except Exception as e:
        print(f"  WARNING: HHFDC income fetch failed ({e}) — county income will not be updated")

    print("\nFetching HUD state median family income...")
    try:
        state_income = fetch_hud_state_mfi()
        state_income.pop("_period", None)
        for key, vals in state_income.items():
            all_prices.setdefault(key, {}).update(vals)
        print(f"  Got income for: {', '.join(state_income.keys())}")
    except Exception as e:
        print(f"  WARNING: HUD state income fetch failed ({e}) — state income will not be updated")

    build_period = "?"
    print("\nFetching DBEDT construction authorization data...")
    try:
        dbedt_data = fetch_dbedt_construction()
        build_period = dbedt_data.pop("_period", "?")
        for key, build_auth in dbedt_data.items():
            all_prices.setdefault(key, {})["buildAuth"] = build_auth
        print(f"  Got buildAuth ({build_period}) for: {', '.join(dbedt_data.keys())}")
    except Exception as e:
        print(f"  WARNING: DBEDT construction fetch failed ({e}) — buildAuth will not be updated")

    return build_period


def _print_summary(all_prices: dict, build_period: str) -> None:
    """Print a formatted table of the latest fetched values."""
    print("\nLatest data:\n")
    print(f"  {'County':<12} {'SFH':>12} {'Condo':>12} {'ContractRent':>13} "
          f"{'AskRent':>10} {'BuildAuth($M)':>14}  {'Period'}")
    print(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*13} {'─'*10} {'─'*14}  {'─'*10}")
    for key in ("State", "Honolulu", "Maui", "Hawaii", "Kauai"):
        if key not in all_prices:
            continue
        v         = all_prices[key]
        sfh       = f"${v['sfhPrice']:>10,}"   if "sfhPrice"   in v else f"{'N/A':>11}"
        condo     = f"${v['condoPrice']:>10,}" if "condoPrice" in v else f"{'N/A':>11}"
        crent     = f"${v['rent']:>11,}"       if "rent"       in v else f"{'N/A':>12}"
        askrent   = f"${v['askRent']:>8,}"     if "askRent"    in v else f"{'N/A':>9}"
        buildauth = f"${v['buildAuth']:>11,}M" if "buildAuth"  in v else f"{'N/A':>13}"
        print(f"  {key:<12} {sfh} {condo} {crent} {askrent} {buildauth}  "
              f"{v.get('period', build_period)}")


def _write_html(
    targets: list[Path],
    all_prices: dict,
    zori_period: str | None,
    bls_rent_period: str | None,
    housing_period: str | None,
    dry_run: bool,
) -> None:
    """Patch and write (or dry-run report) both dashboard HTML files."""
    for target in targets:
        if not target.exists():
            print(f"\nSkipping {target.name} — not found")
            continue
        html    = target.read_text(encoding="utf-8")
        patched = patch_html(html, all_prices)
        patched = patch_periods(patched, zori_period, bls_rent_period, housing_period)
        if patched == html:
            print(f"\n{target.name}: no changes needed — prices already current.")
            continue
        if dry_run:
            print(f"\n[dry-run] would patch {target.name}")
        else:
            target.write_text(patched, encoding="utf-8")
            print(f"\nUpdated {target.name} with latest Redfin prices.")


def main():
    dry_run  = "--dry-run" in sys.argv
    file_idx = sys.argv.index("--file") + 1 if "--file" in sys.argv else None
    targets  = [Path(sys.argv[file_idx])] if file_idx else DEFAULT_FILES

    all_prices = _fetch_sale_prices()

    _, bls_rent_period, bls_rent_yoy, zori_period, zori_yoy_map = _fetch_rents(all_prices)

    # Dev-facing NTR/ATR sanity check (prints table, warns on large divergence).
    audit_rent_nowcast_vs_ntr(
        hnl_cpi_yoy  = bls_rent_yoy,
        hnl_zori_yoy = zori_yoy_map.get("Honolulu"),
    )

    build_period = _fetch_income_and_construction(all_prices)
    _print_summary(all_prices, build_period)

    if dry_run:
        print("\n--dry-run: no files modified")
        return

    housing_period = (all_prices.get("State", {}).get("period") or "")[:7] or None
    _write_html(targets, all_prices, zori_period, bls_rent_period, housing_period, dry_run)


if __name__ == "__main__":
    main()
