#!/usr/bin/env python3
"""Build store market share weights from SNAP retailer data and Census CBP.

Downloads USDA SNAP retailer locator data for Hawaii, classifies stores
into chains using regex patterns, applies format-based size multipliers,
and computes estimated market share per chain per county.

Optionally queries Census County Business Patterns API for employment
data as a cross-validation proxy.

Output: config/store_weights.json

Usage:
    python scripts/build_store_weights.py
    python scripts/build_store_weights.py --snap-csv data/snap/snap_retailers_hi.csv
    python scripts/build_store_weights.py --no-census
"""

import argparse
import csv
import io
import json
import re
import sys
from datetime import date
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
SNAP_CACHE_DIR = PROJECT_ROOT / "data" / "snap"

# USDA SNAP Retailer Locator — ArcGIS Feature Service (public, no token)
# Service: snap_retailer_location_data (NOT Store_Locations, which requires a token)
SNAP_API_URL = (
    "https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services"
    "/snap_retailer_location_data/FeatureServer/0/query"
)
SNAP_API_PARAMS = {
    "where": "State='HI'",
    "outFields": "Store_Name,Store_Street_Address,City,State,Zip_Code,County,Longitude,Latitude,Store_Type",
    "outSR": "4326",
    "f": "json",
}

# Census County Business Patterns API
CENSUS_CBP_URL = "https://api.census.gov/data/{year}/cbp"

FIPS_TO_COUNTY = {
    "001": "hawaii",
    "003": "honolulu",
    "007": "kauai",
    "009": "maui",
}
COUNTY_TO_FIPS = {v: k for k, v in FIPS_TO_COUNTY.items()}

# Known store counts for validation (approximate, from published sources)
KNOWN_COUNTS = {
    "foodland": 32,
    "safeway": 19,
    "times": 24,
    "kta": 6,
    "costco": 7,
}


# ---------------------------------------------------------------------------
# SNAP data download
# ---------------------------------------------------------------------------

def download_snap_data(cache_path: Path) -> list[dict]:
    """Download SNAP retailer data for Hawaii via ArcGIS API.

    Returns list of store dicts with keys:
        Store_Name, Address, City, State, Zip5, County, Longitude, Latitude, Store_Type
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    print("Fetching SNAP retailer data from USDA ArcGIS API...")
    try:
        resp = requests.get(SNAP_API_URL, params=SNAP_API_PARAMS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"WARNING: SNAP API request failed: {exc}")
        print()
        print("Manual fallback: download the SNAP retailer CSV from")
        print("  https://www.fns.usda.gov/snap/retailer-locator/data")
        print("Then re-run with:")
        print(f"  python {sys.argv[0]} --snap-csv <path-to-downloaded-csv>")
        sys.exit(1)

    if "features" not in data:
        print(f"ERROR: Unexpected API response structure. Keys: {list(data.keys())}")
        if "error" in data:
            print(f"  API error: {data['error']}")
        sys.exit(1)

    stores = []
    for feat in data["features"]:
        attrs = feat.get("attributes", {})
        stores.append({
            "Store_Name": (attrs.get("Store_Name") or "").strip(),
            "Address": (attrs.get("Store_Street_Address") or "").strip(),
            "City": (attrs.get("City") or "").strip(),
            "State": (attrs.get("State") or "").strip(),
            "Zip5": str(attrs.get("Zip_Code") or "").strip(),
            "County": (attrs.get("County") or "").strip(),
            "Longitude": attrs.get("Longitude"),
            "Latitude": attrs.get("Latitude"),
            "Store_Type": (attrs.get("Store_Type") or "").strip(),
        })

    print(f"  Downloaded {len(stores)} SNAP retailers for Hawaii")

    # Cache to CSV
    if stores:
        fieldnames = list(stores[0].keys())
        with open(cache_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(stores)
        print(f"  Cached to {cache_path}")

    return stores


def load_snap_csv(csv_path: Path) -> list[dict]:
    """Load SNAP retailer data from a CSV file.

    Handles both our cached format and the official USDA download format
    by normalizing column names.
    """
    print(f"Loading SNAP data from {csv_path}...")
    stores = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Normalize column names — the USDA download may use different headers
        for row in reader:
            # Try multiple known column name variants
            store = {
                "Store_Name": (
                    row.get("Store_Name")
                    or row.get("store_name")
                    or row.get("Store Name")
                    or ""
                ).strip(),
                "Address": (
                    row.get("Address")
                    or row.get("address")
                    or row.get("Address Line 1")
                    or ""
                ).strip(),
                "City": (
                    row.get("City")
                    or row.get("city")
                    or ""
                ).strip(),
                "State": (
                    row.get("State")
                    or row.get("state")
                    or ""
                ).strip(),
                "Zip5": (
                    row.get("Zip5")
                    or row.get("zip5")
                    or row.get("Zip Code")
                    or ""
                ).strip(),
                "County": (
                    row.get("County")
                    or row.get("county")
                    or ""
                ).strip(),
                "Longitude": row.get("Longitude") or row.get("longitude"),
                "Latitude": row.get("Latitude") or row.get("latitude"),
                "Store_Type": (
                    row.get("Store_Type")
                    or row.get("store_type")
                    or row.get("Store Type")
                    or ""
                ).strip(),
            }
            # Filter to Hawaii only (in case the CSV is a full national download)
            if store["State"].upper() in ("HI", "HAWAII", ""):
                stores.append(store)

    print(f"  Loaded {len(stores)} stores")
    return stores


# ---------------------------------------------------------------------------
# Chain classification
# ---------------------------------------------------------------------------

def load_chain_config() -> dict:
    """Load chain classification config."""
    config_path = CONFIG_DIR / "chain_classification.json"
    with open(config_path) as f:
        return json.load(f)


def classify_store(store_name: str, chain_config: dict) -> tuple[str, str]:
    """Classify a store name into a chain ID.

    Returns (chain_id, chain_name). If no match, returns ("other", "Other").
    Tests chains in the order specified by match_order.
    """
    name_upper = store_name.upper().strip()
    match_order = chain_config["match_order"]
    chains = chain_config["chains"]

    for chain_id in match_order:
        chain_def = chains[chain_id]
        for pattern in chain_def["patterns"]:
            if re.search(pattern, name_upper, re.IGNORECASE):
                return chain_id, chain_def["name"]

    return "other", "Other"


def filter_and_classify(
    stores: list[dict],
    chain_config: dict,
) -> list[dict]:
    """Filter SNAP stores to food-relevant types and classify into chains.

    Returns list of dicts with added chain_id, chain_name fields.
    """
    include_types = set(chain_config.get("snap_store_types_include", []))
    exclude_types = set(chain_config.get("snap_store_types_exclude", []))

    # Normalize county names to project keys
    county_name_map = {
        "HONOLULU": "honolulu",
        "MAUI": "maui",
        "HAWAII": "hawaii",
        "KAUAI": "kauai",
        "KALAWAO": None,  # Tiny county on Molokai, skip
    }

    classified = []
    skipped_type = 0
    skipped_county = 0

    for store in stores:
        store_type = store["Store_Type"]

        # Filter by store type if we have type info
        if store_type:
            if exclude_types and store_type in exclude_types:
                skipped_type += 1
                continue
            if include_types and store_type not in include_types:
                skipped_type += 1
                continue

        # Map county
        raw_county = store["County"].upper().strip()
        county_key = county_name_map.get(raw_county)
        if county_key is None:
            skipped_county += 1
            continue

        chain_id, chain_name = classify_store(store["Store_Name"], chain_config)

        classified.append({
            **store,
            "county_key": county_key,
            "chain_id": chain_id,
            "chain_name": chain_name,
        })

    print(f"  Classified {len(classified)} food retailers across 4 counties")
    print(f"  Skipped {skipped_type} by store type, {skipped_county} by county")

    return classified


# ---------------------------------------------------------------------------
# Weight computation
# ---------------------------------------------------------------------------

def compute_weights(
    classified_stores: list[dict],
    chain_config: dict,
) -> dict:
    """Compute per-chain per-county market share weights.

    Returns dict: county -> chain_id -> weight (summing to 1.0 per county).
    Also returns raw_counts and weighted_counts for diagnostics.
    """
    chains = chain_config["chains"]
    other_mult = chain_config.get("other_format_multiplier", 0.3)

    # Count stores: county -> chain_id -> count
    raw_counts: dict[str, dict[str, int]] = {}
    for store in classified_stores:
        county = store["county_key"]
        chain_id = store["chain_id"]
        raw_counts.setdefault(county, {})
        raw_counts[county][chain_id] = raw_counts[county].get(chain_id, 0) + 1

    # Apply format multipliers
    weighted_counts: dict[str, dict[str, float]] = {}
    for county, chain_counts in raw_counts.items():
        weighted_counts[county] = {}
        for chain_id, count in chain_counts.items():
            if chain_id == "other":
                mult = other_mult
            else:
                mult = chains.get(chain_id, {}).get("format_multiplier", 1.0)
            weighted_counts[county][chain_id] = count * mult

    # Normalize to shares
    weights: dict[str, dict[str, float]] = {}
    for county in sorted(weighted_counts.keys()):
        total = sum(weighted_counts[county].values())
        if total == 0:
            continue
        weights[county] = {}
        for chain_id in sorted(weighted_counts[county].keys()):
            weights[county][chain_id] = round(
                weighted_counts[county][chain_id] / total, 4
            )

    return {
        "weights": weights,
        "raw_counts": raw_counts,
        "weighted_counts": weighted_counts,
    }


# ---------------------------------------------------------------------------
# Census CBP validation
# ---------------------------------------------------------------------------

def fetch_census_cbp() -> dict[str, dict] | None:
    """Query Census County Business Patterns API for NAICS 445110.

    Returns dict: county_key -> {establishments, employment, payroll}
    """
    # Try 2023 first, then 2022
    for year in [2023, 2022]:
        url = CENSUS_CBP_URL.format(year=year)
        params = {
            "get": "ESTAB,EMP,PAYANN",
            "for": "county:001,003,007,009",
            "in": "state:15",
            "NAICS2017": "445110",
        }
        print(f"  Querying Census CBP {year} for NAICS 445110...")
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  WARNING: Census API {year} failed: {exc}")
            continue

        if not data or len(data) < 2:
            print(f"  WARNING: No data returned for {year}")
            continue

        # Parse response: first row is headers, rest are data
        headers = data[0]
        results = {}
        for row in data[1:]:
            row_dict = dict(zip(headers, row))
            county_fips = row_dict.get("county", "")
            county_key = FIPS_TO_COUNTY.get(county_fips)
            if county_key:
                results[county_key] = {
                    "year": year,
                    "establishments": int(row_dict.get("ESTAB", 0)),
                    "employment": int(row_dict.get("EMP", 0)),
                    "payroll_thousands": int(row_dict.get("PAYANN", 0)),
                }

        if results:
            print(f"  Got CBP data for {len(results)} counties ({year})")
            return results

    print("  WARNING: Could not fetch Census CBP data for any year")
    return None


# ---------------------------------------------------------------------------
# Validation & reporting
# ---------------------------------------------------------------------------

def print_validation_report(
    result: dict,
    cbp_data: dict | None,
    classified_stores: list[dict],
) -> None:
    """Print detailed validation report."""
    weights = result["weights"]
    raw_counts = result["raw_counts"]

    print()
    print("=" * 70)
    print("STORE WEIGHTS VALIDATION REPORT")
    print("=" * 70)

    # --- Statewide chain totals vs known counts ---
    print("\n1. STATEWIDE CHAIN COUNTS (SNAP vs Published)")
    print("-" * 50)
    chain_totals: dict[str, int] = {}
    for county_counts in raw_counts.values():
        for chain_id, count in county_counts.items():
            chain_totals[chain_id] = chain_totals.get(chain_id, 0) + count

    print(f"  {'Chain':<20s} {'SNAP':>6s} {'Published':>10s} {'Delta':>8s}")
    for chain_id in sorted(chain_totals.keys()):
        snap_n = chain_totals[chain_id]
        known_n = KNOWN_COUNTS.get(chain_id)
        if known_n:
            delta = snap_n - known_n
            flag = " !!!" if abs(delta) > known_n * 0.3 else ""
            print(f"  {chain_id:<20s} {snap_n:>6d} {known_n:>10d} {delta:>+8d}{flag}")
        else:
            print(f"  {chain_id:<20s} {snap_n:>6d} {'n/a':>10s}")

    # --- Per-county breakdown ---
    print("\n2. PER-COUNTY CHAIN BREAKDOWN")
    print("-" * 50)
    for county in ["honolulu", "maui", "hawaii", "kauai"]:
        if county not in weights:
            continue
        print(f"\n  {county.upper()}")
        county_raw = raw_counts.get(county, {})
        county_wt = weights[county]
        print(f"    {'Chain':<20s} {'Stores':>7s} {'Weight':>8s}")
        for chain_id in sorted(county_wt.keys(), key=lambda c: -county_wt[c]):
            n = county_raw.get(chain_id, 0)
            w = county_wt[chain_id]
            print(f"    {chain_id:<20s} {n:>7d} {w:>8.1%}")

    # --- Weight sum check ---
    print("\n3. WEIGHT SUM CHECK")
    print("-" * 50)
    all_ok = True
    for county in sorted(weights.keys()):
        total = sum(weights[county].values())
        status = "OK" if abs(total - 1.0) < 0.002 else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"  {county:<12s} sum = {total:.4f}  [{status}]")
    if all_ok:
        print("  All counties pass.")

    # --- CBP cross-validation ---
    if cbp_data:
        print("\n4. CENSUS CBP CROSS-VALIDATION")
        print("-" * 50)
        # Compute weight-implied "size" per county (sum of weighted counts)
        total_weighted = sum(
            sum(result["weighted_counts"].get(c, {}).values())
            for c in weights
        )
        total_emp = sum(d["employment"] for d in cbp_data.values())

        print(f"  {'County':<12s} {'Wt Share':>10s} {'Emp Share':>10s} {'Ratio':>8s}")
        for county in ["honolulu", "maui", "hawaii", "kauai"]:
            wt_share = (
                sum(result["weighted_counts"].get(county, {}).values()) / total_weighted
                if total_weighted > 0
                else 0
            )
            emp = cbp_data.get(county, {}).get("employment", 0)
            emp_share = emp / total_emp if total_emp > 0 else 0
            ratio = wt_share / emp_share if emp_share > 0 else float("inf")
            flag = " !!!" if ratio < 0.5 or ratio > 2.0 else ""
            print(
                f"  {county:<12s} {wt_share:>10.1%} {emp_share:>10.1%} {ratio:>8.2f}{flag}"
            )
        print("  (Ratio near 1.0 = good alignment between SNAP counts and CBP employment)")

    # --- Unclassified stores sample ---
    other_stores = [s for s in classified_stores if s["chain_id"] == "other"]
    if other_stores:
        print(f"\n5. UNCLASSIFIED STORES ({len(other_stores)} total)")
        print("-" * 50)
        # Show a sample
        sample = other_stores[:20]
        for s in sample:
            print(f"  {s['county_key']:<10s} {s['Store_Name']:<40s} {s['Store_Type']}")
        if len(other_stores) > 20:
            print(f"  ... and {len(other_stores) - 20} more")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_store_weights(
    result: dict,
    cbp_data: dict | None,
    output_path: Path,
) -> None:
    """Write config/store_weights.json."""
    output = {
        "version": "1.0.0",
        "generated": date.today().isoformat(),
        "methodology": (
            "Estimated grocery market share per chain per county. "
            "Derived from USDA SNAP retailer counts weighted by "
            "store-format size multipliers, normalized per county."
        ),
        "data_sources": {
            "snap_retailer_locator": "https://www.fns.usda.gov/snap/retailer-locator/data",
            "census_cbp": "https://api.census.gov/data/2023/cbp",
        },
        "weights": result["weights"],
        "diagnostics": {
            "raw_counts": result["raw_counts"],
        },
    }
    if cbp_data:
        output["diagnostics"]["census_cbp_employment"] = {
            county: d["employment"] for county, d in cbp_data.items()
        }
        output["diagnostics"]["census_cbp_year"] = next(
            iter(cbp_data.values())
        ).get("year")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")

    print(f"\nWrote {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build store market share weights from SNAP + Census data"
    )
    parser.add_argument(
        "--snap-csv",
        help="Path to pre-downloaded SNAP retailer CSV (skip API download)",
    )
    parser.add_argument(
        "--no-census",
        action="store_true",
        help="Skip Census CBP API query",
    )
    parser.add_argument(
        "--output",
        default=str(CONFIG_DIR / "store_weights.json"),
        help="Output path for store_weights.json",
    )
    args = parser.parse_args()

    # Step 1: Get SNAP data
    if args.snap_csv:
        stores = load_snap_csv(Path(args.snap_csv))
    else:
        cache_path = SNAP_CACHE_DIR / "snap_retailers_hi.csv"
        if cache_path.exists():
            print(f"Found cached SNAP data at {cache_path}")
            stores = load_snap_csv(cache_path)
        else:
            stores = download_snap_data(cache_path)

    if not stores:
        print("ERROR: No SNAP stores loaded. Exiting.")
        sys.exit(1)

    # Step 2: Load chain config and classify
    chain_config = load_chain_config()
    classified = filter_and_classify(stores, chain_config)

    if not classified:
        print("ERROR: No stores passed filtering. Check store types and counties.")
        sys.exit(1)

    # Step 3: Compute weights
    result = compute_weights(classified, chain_config)

    # Step 4: Census CBP (optional)
    cbp_data = None
    if not args.no_census:
        print("\nFetching Census County Business Patterns for validation...")
        cbp_data = fetch_census_cbp()
        if cbp_data:
            # Cache it
            cbp_cache = SNAP_CACHE_DIR / "census_cbp_445110.json"
            cbp_cache.parent.mkdir(parents=True, exist_ok=True)
            with open(cbp_cache, "w") as f:
                json.dump(cbp_data, f, indent=2)

    # Step 5: Validate and report
    print_validation_report(result, cbp_data, classified)

    # Step 6: Write output
    write_store_weights(result, cbp_data, Path(args.output))

    print("\nDone.")


if __name__ == "__main__":
    main()
