#!/usr/bin/env python3
"""Import manually-collected baseline prices from CSV files.

Expected CSV format (one file per store):
    slot_id,product_name,price,size_qty,size_unit,is_substitution,substitution_note
    GRAIN-01,Nishiki Calrose Rice 5 lb,7.49,5,lb,false,
    MEAT-08,SPAM Classic 12 oz,3.89,12,oz,false,
    ...

Usage:
    python scripts/init_baseline.py data/baseline/foodland_honolulu.csv
    python scripts/init_baseline.py data/baseline/  # import all CSVs in directory
"""

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import BaselinePrice, BasketConfig, StoreConfig


def parse_store_info(filename: str, store_config: StoreConfig) -> dict | None:
    """Extract chain and store_id from filename like 'foodland_honolulu.csv'."""
    stem = Path(filename).stem  # e.g. "foodland_honolulu"
    for chain_id, chain_data in store_config.chains.items():
        for store in chain_data["stores"]:
            # Match by store_id (e.g. "foodland-honolulu") with underscore variant
            if stem == store["store_id"].replace("-", "_"):
                return {
                    "chain": chain_id,
                    "store_id": store["store_id"],
                    "county": store["county"],
                    "geoid": store_config.get_geoid(store["county"]),
                }
    return None


def import_csv(csv_path: Path, basket: BasketConfig, store_config: StoreConfig) -> list[BaselinePrice]:
    """Import a single baseline CSV file."""
    store_info = parse_store_info(csv_path.name, store_config)
    if store_info is None:
        print(f"  WARNING: Could not match '{csv_path.name}' to a store. "
              f"Expected format: {{chain}}_{{location}}.csv (e.g. foodland_honolulu.csv)")
        return []

    prices = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            slot_id = row["slot_id"].strip()
            item = basket.get_item(slot_id)
            if item is None:
                print(f"  WARNING: Unknown slot_id '{slot_id}' — skipping")
                continue

            price = float(row["price"])
            size_qty = float(row.get("size_qty", item["size_qty"]))
            size_unit = row.get("size_unit", item["size_unit"]).strip()
            per_unit = price / size_qty if size_qty > 0 else 0.0
            is_sub = row.get("is_substitution", "false").strip().lower() in ("true", "1", "yes")

            bp = BaselinePrice(
                slot_id=slot_id,
                chain=store_info["chain"],
                store_id=store_info["store_id"],
                county=store_info["county"],
                geoid=store_info["geoid"],
                date=row.get("date", date.today().isoformat()),
                product_name=row.get("product_name", item["description"]).strip(),
                price=price,
                size_qty=size_qty,
                size_unit=size_unit,
                per_unit_price=round(per_unit, 4),
                is_substitution=is_sub,
                substitution_note=row.get("substitution_note", "").strip() or None,
            )
            prices.append(bp)

    return prices


def save_consolidated(prices: list[BaselinePrice], output_path: Path) -> None:
    """Save all baseline prices to a single consolidated CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "slot_id", "chain", "store_id", "county", "geoid", "date",
        "product_name", "price", "size_qty", "size_unit", "per_unit_price",
        "is_substitution", "substitution_note",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for bp in sorted(prices, key=lambda p: (p.chain, p.county, p.slot_id)):
            writer.writerow({
                "slot_id": bp.slot_id,
                "chain": bp.chain,
                "store_id": bp.store_id,
                "county": bp.county,
                "geoid": bp.geoid,
                "date": bp.date,
                "product_name": bp.product_name,
                "price": bp.price,
                "size_qty": bp.size_qty,
                "size_unit": bp.size_unit,
                "per_unit_price": bp.per_unit_price,
                "is_substitution": bp.is_substitution,
                "substitution_note": bp.substitution_note or "",
            })


def main():
    parser = argparse.ArgumentParser(description="Import baseline price CSVs")
    parser.add_argument("path", help="CSV file or directory of CSV files")
    parser.add_argument(
        "--output", "-o",
        default="data/baseline/consolidated_baseline.csv",
        help="Output path for consolidated CSV (default: data/baseline/consolidated_baseline.csv)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent
    basket = BasketConfig.load()
    store_config = StoreConfig.load()

    input_path = Path(args.path)
    if not input_path.is_absolute():
        input_path = project_root / input_path

    csv_files = []
    if input_path.is_dir():
        csv_files = sorted(input_path.glob("*.csv"))
        # Exclude the consolidated output file itself
        csv_files = [f for f in csv_files if "consolidated" not in f.name]
    elif input_path.is_file():
        csv_files = [input_path]
    else:
        print(f"ERROR: {input_path} does not exist")
        sys.exit(1)

    if not csv_files:
        print(f"No CSV files found in {input_path}")
        sys.exit(1)

    all_prices = []
    for csv_file in csv_files:
        print(f"Importing {csv_file.name}...")
        prices = import_csv(csv_file, basket, store_config)
        print(f"  {len(prices)} prices imported")
        all_prices.extend(prices)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = project_root / output_path

    save_consolidated(all_prices, output_path)
    print(f"\nConsolidated {len(all_prices)} prices → {output_path}")

    # Summary
    by_chain = {}
    by_county = {}
    for p in all_prices:
        by_chain[p.chain] = by_chain.get(p.chain, 0) + 1
        by_county[p.county] = by_county.get(p.county, 0) + 1
    print("\nBy chain:", json.dumps(by_chain, indent=2))
    print("By county:", json.dumps(by_county, indent=2))


if __name__ == "__main__":
    main()
