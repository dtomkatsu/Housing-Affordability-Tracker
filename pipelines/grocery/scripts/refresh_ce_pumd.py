#!/usr/bin/env python3
"""
refresh_ce_pumd.py
------------------
Annually-run script that downloads the BLS Consumer Expenditure Public Use
Microdata (CE PUMD) interview-survey ZIPs for the 5 most recent published
years, extracts a "typical Urban Honolulu household monthly food-at-home
spending" figure, and writes it as a side-statistic JSON consumed by the
grocery pipeline.

This is NOT part of the regular grocery-price-updater run. It's a slow,
network-heavy refresh — run once a year (target: October, after BLS publishes
the latest interview-survey microdata).

Usage
~~~~~
    python3 pipelines/grocery/scripts/refresh_ce_pumd.py
    python3 pipelines/grocery/scripts/refresh_ce_pumd.py --years 2019 2020 2021 2022 2023
    python3 pipelines/grocery/scripts/refresh_ce_pumd.py --keep-raw     # keep extracted CSVs

Output
~~~~~~
    pipelines/grocery/data/pumd_honolulu_monthly.json
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
GROCERY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GROCERY_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from common.http_client import fetch_bytes  # noqa: E402
from src.pumd_extractor import (              # noqa: E402
    extract_honolulu_fah,
    pool_years,
    project_to_neighbor_islands,
    HONOLULU_PSU_CODES,
)
from src.cpi_fetcher import fetch_cpi_data    # noqa: E402

PUMD_URL_TEMPLATE = "https://www.bls.gov/cex/pumd/data/comma/intrvw{yy}.zip"

# Default 5-year window (2019-2023 — 2024 expected ~Sep 2026)
DEFAULT_YEARS = [2019, 2020, 2021, 2022, 2023]

RAW_DIR = GROCERY_ROOT / "data" / "pumd_raw"
OUT_JSON = GROCERY_ROOT / "data" / "pumd_honolulu_monthly.json"
COUNTY_CSV = GROCERY_ROOT / "data" / "output" / "county_comparison.csv"

# Honolulu food CPI (CUURS49ASAF11) is bimonthly. We average to annual for
# inflation adjustment between PUMD years.
FOOD_CPI_SERIES = "CUURS49ASAF11"


# ---------------------------------------------------------------------------
# Download + unpack
# ---------------------------------------------------------------------------
def download_pumd_year(year: int, raw_dir: Path) -> Path:
    """Download and unpack the year's interview-survey ZIP. Returns the
    extracted directory path."""
    yy = str(year)[2:]
    url = PUMD_URL_TEMPLATE.format(yy=yy)
    out_dir = raw_dir / f"intrvw{yy}"
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / "_extracted.flag"
    if marker.exists():
        print(f"  [{year}] already extracted at {out_dir}")
        return out_dir
    print(f"  [{year}] downloading {url} …")
    raw = fetch_bytes(url, timeout=300)
    print(f"  [{year}] unzipping {len(raw):,} bytes …")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        z.extractall(out_dir)
    marker.write_text(date.today().isoformat())
    return out_dir


# ---------------------------------------------------------------------------
# CSV loaders (pandas — required)
# ---------------------------------------------------------------------------
def _find_files(root: Path, prefix_re: str) -> list[Path]:
    """Recursively find FMLI*/MTBI* CSV files (the layout varies by year:
    sometimes intrvw{yy}/intrvw{yy}/<file>.csv, sometimes flat)."""
    pat = re.compile(prefix_re, re.I)
    return sorted(p for p in root.rglob("*.csv") if pat.match(p.name))


def load_year(year: int, year_dir: Path):
    """Load and concatenate the 4 quarterly FMLI and MTBI CSVs for one year.

    PUMD layout: FMLI{Y}{Q}.csv where Y is last digit of year and Q is 1-4
    (e.g. FMLI191, FMLI192, FMLI193, FMLI194 plus FMLI201 for the trailing
    Q1 of next year). The trailing-quarter file is excluded — it's that
    year's first quarter mislabeled by BLS for cross-year continuity.
    """
    import pandas as pd

    yd = str(year)[-1]
    fmli_files = _find_files(year_dir, rf"FMLI{yd}[1-4]\.csv$")
    mtbi_files = _find_files(year_dir, rf"MTBI{yd}[1-4]\.csv$")

    if not fmli_files or not mtbi_files:
        raise FileNotFoundError(
            f"No FMLI/MTBI files found in {year_dir} for year {year}. "
            f"Searched recursively. Found FMLI: {fmli_files}, MTBI: {mtbi_files}"
        )

    print(f"  [{year}] loading {len(fmli_files)} FMLI + {len(mtbi_files)} MTBI quarters …")
    fmli = pd.concat([pd.read_csv(f, low_memory=False) for f in fmli_files], ignore_index=True)
    mtbi = pd.concat([pd.read_csv(f, low_memory=False) for f in mtbi_files], ignore_index=True)

    # Normalize column names (some years use lower-case)
    fmli.columns = [c.upper() for c in fmli.columns]
    mtbi.columns = [c.upper() for c in mtbi.columns]
    return fmli, mtbi


# ---------------------------------------------------------------------------
# Honolulu food CPI annual averages
# ---------------------------------------------------------------------------
def fetch_food_cpi_annual(years: list[int]) -> dict[int, float]:
    """Returns {year: annual_avg_index}. Uses the existing cpi_fetcher.

    cpi_fetcher.fetch_cpi_data returns:
       { series_id: [ {year, period, value, periodName}, ... ] }
    We average all periods per year.
    """
    print("  Fetching Honolulu food CPI for inflation adjustment …")
    raw = fetch_cpi_data([FOOD_CPI_SERIES],
                         start_year=min(years),
                         end_year=max(years) + 1)
    points = raw.get(FOOD_CPI_SERIES, [])
    by_year: dict[int, list[float]] = {}
    for p in points:
        try:
            yr = int(p["year"])
            v = float(p["value"])
        except (KeyError, ValueError, TypeError):
            continue
        by_year.setdefault(yr, []).append(v)
    return {yr: sum(vs) / len(vs) for yr, vs in by_year.items() if vs}


# ---------------------------------------------------------------------------
# Basket totals from the receipt pipeline
# ---------------------------------------------------------------------------
def load_basket_totals(county_csv: Path) -> dict[str, float]:
    """Sum each county column to get a comparable per-county basket total."""
    import pandas as pd
    if not county_csv.exists():
        raise FileNotFoundError(
            f"{county_csv} not found — run the grocery pipeline first to "
            "generate county_comparison.csv (the basket gradient anchor)."
        )
    df = pd.read_csv(county_csv)
    # Columns are slot_id, item, hawaii, honolulu, kauai, maui, unit
    out = {}
    for c, html_key in (("honolulu", "Honolulu"), ("hawaii", "Hawaii"),
                        ("maui", "Maui"), ("kauai", "Kauai")):
        if c in df.columns:
            out[html_key] = float(pd.to_numeric(df[c], errors="coerce").sum())
    return out


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def run(years: list[int], target_year: int, *, keep_raw: bool) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    food_cpi = fetch_food_cpi_annual(years + [target_year])

    per_year_results: list[dict] = []
    for year in years:
        try:
            yd = download_pumd_year(year, RAW_DIR)
            fmli, mtbi = load_year(year, yd)
        except Exception as exc:
            print(f"  [{year}] SKIP: {exc}")
            continue
        result = extract_honolulu_fah(
            fmli, mtbi,
            fmli_year=year,
            food_cpi_annual=food_cpi,
            target_year=target_year,
        )
        n = result["n_total"]
        m = result["overall"].monthly_fah
        print(f"  [{year}] Honolulu PSU: n={n}, monthly FAH = ${m:.2f}")
        per_year_results.append(result)

    if not per_year_results:
        raise RuntimeError("No PUMD years extracted successfully.")

    pooled = pool_years(per_year_results)
    overall = pooled["overall"]
    by_size = pooled["by_size"]

    # Project to neighbor islands
    basket_totals = load_basket_totals(COUNTY_CSV)
    family4_value = (
        by_size["4+"].monthly_fah
        if by_size.get("4+") and by_size["4+"].monthly_fah > 0
        else overall.monthly_fah
    )
    by_county_family4 = project_to_neighbor_islands(family4_value, basket_totals)

    out = {
        "source": f"BLS CE PUMD {min(years)}-{max(years)} pooled",
        "psu":    "Urban Honolulu, HI",
        "psu_codes_searched": sorted(HONOLULU_PSU_CODES),
        "n_households_total":     pooled["n_total"],
        "as_of_period":           f"{target_year}-12",
        "method":                 f"{len(years)}y_pooled_finlwt21_inflated_to_{target_year}",
        "by_county_monthly_family4_fah": {
            k: round(v, 2) for k, v in by_county_family4.items()
        },
        "honolulu_by_size_monthly_fah": {
            "1":  round(by_size["1"].monthly_fah, 2),
            "2":  round(by_size["2"].monthly_fah, 2),
            "3":  round(by_size["3"].monthly_fah, 2),
            "4+": round(by_size["4+"].monthly_fah, 2),
        },
        "honolulu_overall_monthly_fah":   round(overall.monthly_fah, 2),
        "honolulu_ci_95_overall":         [round(overall.ci_95[0], 2), round(overall.ci_95[1], 2)],
        "honolulu_ci_95_family4":         [round(by_size["4+"].ci_95[0], 2),
                                           round(by_size["4+"].ci_95[1], 2)],
        "years_pooled":                   pooled["years"],
        "note": (
            "Honolulu figure is directly measured from PUMD; neighbor-island "
            "values are scaled by the receipt-derived basket gradient "
            "(basket_total[county] / basket_total[Honolulu]). State value is "
            "a population-weighted average of the four county estimates. "
            "This is a side-statistic shown alongside the receipt-derived "
            "monthlyFamily4; it does not feed pricing."
        ),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--years", nargs="+", type=int, default=DEFAULT_YEARS,
                    help=f"Years to pool (default: {DEFAULT_YEARS})")
    ap.add_argument("--target-year", type=int, default=None,
                    help="Inflate to this year (default: most recent year)")
    ap.add_argument("--out", type=Path, default=OUT_JSON,
                    help=f"Output JSON path (default: {OUT_JSON})")
    ap.add_argument("--keep-raw", action="store_true",
                    help="Don't delete raw extracted CSVs after run")
    args = ap.parse_args()

    target_year = args.target_year or max(args.years)
    print(f"Refreshing CE PUMD pooled estimate")
    print(f"  Years     : {args.years}")
    print(f"  Inflate-to: {target_year}")
    print(f"  Output    : {args.out}")

    try:
        out = run(args.years, target_year, keep_raw=args.keep_raw)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nWrote {args.out}")
    print(f"  Honolulu monthly FAH (family of 4): "
          f"${out['by_county_monthly_family4_fah'].get('Honolulu', 0):.2f}")
    print(f"  State    monthly FAH (family of 4): "
          f"${out['by_county_monthly_family4_fah'].get('State', 0):.2f}")
    print(f"  N households pooled: {out['n_households_total']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
