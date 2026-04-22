#!/usr/bin/env python3
"""Main CLI: fetch CPI data, adjust baseline prices, generate output.

Uses src.pipeline.run_pipeline() for shared compute logic.

Usage:
    python scripts/update_prices.py                     # adjust to current month
    python scripts/update_prices.py --month 2026-06     # adjust to specific month
    python scripts/update_prices.py --no-fetch          # use cached CPI data
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline import run_pipeline, GET_TAX_RATE
from src.output import (
    generate_county_comparison_csv,
    generate_chain_comparison_csv,
    generate_household_csv,
    generate_county_bar_chart,
    generate_household_bar_chart,
)


def main():
    parser = argparse.ArgumentParser(description="Update prices using CPI adjustment")
    parser.add_argument("--month", help="Target month as YYYY-MM (default: current month)")
    parser.add_argument(
        "--baseline",
        default="data/baseline/consolidated_baseline.csv",
        help="Path to consolidated baseline CSV",
    )
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="Use cached CPI data instead of fetching fresh",
    )
    parser.add_argument(
        "--output-dir", default="data/output",
        help="Output directory for tables and charts",
    )
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent

    # Parse target date
    if args.month:
        year, month = args.month.split("-")
        target_date = date(int(year), int(month), 15)
    else:
        today = date.today()
        target_date = date(today.year, today.month, 15)

    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = project_root / baseline_path

    if not baseline_path.exists():
        print(f"ERROR: Baseline file not found: {baseline_path}")
        print("Run 'python scripts/init_baseline.py' first to import baseline prices.")
        sys.exit(1)

    print(f"Target date: {target_date.isoformat()}")

    # Run pipeline
    result = run_pipeline(
        target_date=target_date,
        baseline_path=baseline_path,
        no_fetch=args.no_fetch,
    )

    adjusted     = result["adjusted"]
    county_prices = result["county_prices"]
    estimates    = result["estimates"]
    basket       = result["basket"]

    print(f"Loaded {len(result['baseline_dates'])} baseline date(s)")
    print(f"Generated {len(adjusted)} adjusted prices")
    print(f"Collapsed to {len(county_prices)} weighted county prices")
    print(f"Generated {len(estimates)} household estimates")

    # Generate output files
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir

    print("\nGenerating output...")

    csv1 = generate_county_comparison_csv(county_prices, basket, output_dir / "county_comparison.csv")
    print(f"  County comparison: {csv1}")

    csv1b = generate_chain_comparison_csv(adjusted, basket, output_dir / "chain_comparison.csv")
    print(f"  Chain comparison: {csv1b}")

    csv2 = generate_household_csv(estimates, output_dir / "household_estimates.csv", get_tax_rate=GET_TAX_RATE)
    print(f"  Household estimates: {csv2}")

    chart1 = generate_county_bar_chart(county_prices, output_dir / "county_comparison.png", get_tax_rate=GET_TAX_RATE)
    print(f"  County chart: {chart1}")

    chart2 = generate_household_bar_chart(estimates, output_dir / "household_costs.png", get_tax_rate=GET_TAX_RATE)
    print(f"  Household chart: {chart2}")

    # Print summary
    print("\n" + "=" * 70)
    print("COUNTY BASKET TOTALS (Pre-Tax / With 4.5% GET Tax)")
    print("=" * 70)
    county_totals: dict[str, float] = {}
    for ap in county_prices:
        county_totals[ap.county] = county_totals.get(ap.county, 0.0) + ap.adjusted_price
    for county in sorted(county_totals):
        pretax = county_totals[county]
        with_tax = round(pretax * (1 + GET_TAX_RATE), 2)
        print(f"  {county.title():12s} ${pretax:>8.2f} / ${with_tax:>8.2f}")

    print("\n" + "=" * 70)
    print("HOUSEHOLD COST ESTIMATES (Pre-Tax / With 4.5% GET Tax)")
    print("=" * 70)
    for est in sorted(estimates, key=lambda e: (e.county, e.household_type)):
        with_tax = round(est.household_cost * (1 + GET_TAX_RATE), 2)
        print(f"  {est.county.title():12s} {est.household_label:35s} ${est.household_cost:>8.2f} / ${with_tax:>8.2f}")

    print(f"\nDone. Output saved to {output_dir}/")


if __name__ == "__main__":
    main()
