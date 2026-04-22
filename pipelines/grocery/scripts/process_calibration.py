#!/usr/bin/env python3
"""Process the Foodland in-store vs Instacart calibration CSV.

Reads the raw comparison file, computes markup ratios per item,
saves the calibration table, and extracts the in-store prices
as the Foodland Honolulu baseline.

Usage:
    python scripts/process_calibration.py data/baseline/foodland_instacart_calibration_raw.csv
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import BasketConfig

# Mapping from the calibration CSV product name to basket slot_id
# Based on what was actually found at Foodland Honolulu
PRODUCT_TO_SLOT = {
    # Grains
    "Hinode Calrose Rice, 20 lb":                    ("GRAIN-01", 20.0,   "lb"),
    "Cheerios, 12 oz":                               ("GRAIN-05", 12.0,   "oz"),
    "Barilla Spaghetti, 16 oz":                      ("GRAIN-07", 16.0,   "oz"),
    "Health and Harvest Bread, White, 22 oz":        ("GRAIN-03", 22.0,   "oz"),
    # Vegetables
    "Russet potatoes, 5 lb bag":                     ("VEG-01",   5.0,    "lb"),
    "Roma tomatoes, 1 lb":                           ("VEG-06",   1.0,    "lb"),
    "Del Monte Whole Kernel Corn, 15.25 oz":         ("VEG-08",   15.25,  "oz"),
    "Taylor Farms Baby Spinach, 6 oz":               ("VEG-04",   6.0,    "oz"),
    # Fruits
    "Bananas, 1 lb":                                 ("FRUIT-01", 1.0,    "lb"),
    "Dole Pineapple Chunks in Juice, 20 oz":         ("FRUIT-05", 20.0,   "oz"),
    "Fuji Apple, 1 lb":                              ("FRUIT-03", 1.0,    "lb"),
    "Tropicana No Pulp Orange Juice, 89 oz":         ("FRUIT-08", 89.0,   "fl_oz"),
    # Dairy
    "Sunhearth Milk, 1 gallon":                      ("DAIRY-01", 128.0,  "fl_oz"),
    "Land O'Lakes Salted Butter, 1 lb":              ("DAIRY-06", 16.0,   "oz"),
    "Farm Pack Mainland Eggs, Large, 12 each":       ("DAIRY-07", 12.0,   "ct"),
    "Kraft SIngles American Cheese Slices":          ("DAIRY-03", 12.0,   "oz"),   # 16-slice/12 oz pack; no size on label — assumed from $5.19 price
    # Meat / Protein
    "Ground beef 80/20, ~1 lb":                      ("MEAT-01",  1.0,    "lb"),
    "Boneless skinless chicken breast, 1 lb":        ("MEAT-04",  1.0,    "lb"),
    "SPAM Classic, 12 oz":                           ("MEAT-08",  12.0,   "oz"),
    "StarKist Chunk Light Tuna, 5 oz":               ("FISH-01",  5.0,    "oz"),
    "Jif Creamy Peanut Butter, 16 oz":               ("PROT-04",  16.0,   "oz"),
    # Fats / Oils
    "Wesson Vegetable Oil, 40 oz":                   ("FAT-01",   40.0,   "fl_oz"),
    # Beverages
    "Folgers Classic Roast, 25.9 oz":                ("BEV-03",   25.9,   "oz"),
    # Sugars
    "C&H Granulated Sugar, 4 lb":                    ("SWEET-01", 4.0,    "lb"),
    # Prepared
    "Campbell's Chicken Noodle Soup, 10.75 oz":      ("PREP-01",  10.75,  "oz"),
    "Maruchan Ramen Soy Sauce, 2.25 oz":             ("PREP-03",  2.25,   "oz"),
}


def parse_price(s: str) -> float:
    return float(s.strip().replace("$", "").replace(",", ""))


def main():
    if len(sys.argv) < 2:
        raw_path = Path("data/baseline/foodland_instacart_calibration_raw.csv")
    else:
        raw_path = Path(sys.argv[1])

    project_root = Path(__file__).parent.parent
    if not raw_path.is_absolute():
        raw_path = project_root / raw_path

    # Read raw calibration data
    rows = []
    with open(raw_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    # Build calibration table with markup ratios
    cal_rows = []
    instore_rows = []

    for row in rows:
        product = row["Product"].strip()
        in_store = parse_price(row["In-Store"])
        instacart = parse_price(row["Instacart"])
        markup_ratio = round(instacart / in_store, 4)
        markup_pct = round((markup_ratio - 1) * 100, 1)

        slot_info = PRODUCT_TO_SLOT.get(product)
        if slot_info is None:
            print(f"  WARNING: no slot mapping for '{product}' — skipping")
            continue

        slot_id, size_qty, size_unit = slot_info

        cal_rows.append({
            "slot_id": slot_id,
            "product_name": product,
            "in_store_price": in_store,
            "instacart_price": instacart,
            "markup_ratio": markup_ratio,
            "markup_pct": markup_pct,
            "size_qty": size_qty,
            "size_unit": size_unit,
        })

        instore_rows.append({
            "slot_id": slot_id,
            "product_name": product,
            "price": in_store,
            "size_qty": size_qty,
            "size_unit": size_unit,
            "is_substitution": "false",
            "substitution_note": "",
            "date": "2026-04-10",
        })

    # Save calibration table
    cal_path = project_root / "data/baseline/instacart_calibration.csv"
    cal_fields = ["slot_id", "product_name", "in_store_price", "instacart_price",
                  "markup_ratio", "markup_pct", "size_qty", "size_unit"]
    with open(cal_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cal_fields)
        writer.writeheader()
        for r in sorted(cal_rows, key=lambda x: x["slot_id"]):
            writer.writerow(r)
    print(f"Calibration table saved: {cal_path}")

    # Save Foodland Honolulu in-store baseline
    instore_path = project_root / "data/baseline/foodland_honolulu.csv"
    instore_fields = ["slot_id", "product_name", "price", "size_qty", "size_unit",
                      "is_substitution", "substitution_note", "date"]
    with open(instore_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=instore_fields)
        writer.writeheader()
        for r in sorted(instore_rows, key=lambda x: x["slot_id"]):
            writer.writerow(r)
    print(f"Foodland Honolulu baseline saved: {instore_path}")

    # Print markup summary
    markups = [r["markup_pct"] for r in cal_rows]
    avg = sum(markups) / len(markups)
    print(f"\nInstacart markup summary ({len(markups)} items):")
    print(f"  Average:  {avg:.1f}%")
    print(f"  Min:      {min(markups):.1f}%  ({cal_rows[markups.index(min(markups))]['product_name']})")
    print(f"  Max:      {max(markups):.1f}%  ({cal_rows[markups.index(max(markups))]['product_name']})")
    print(f"\nPer-item markups:")
    for r in sorted(cal_rows, key=lambda x: -x["markup_pct"]):
        print(f"  {r['markup_pct']:>6.1f}%  {r['product_name']}")


if __name__ == "__main__":
    main()
