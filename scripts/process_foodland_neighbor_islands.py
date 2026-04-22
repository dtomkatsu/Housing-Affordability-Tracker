#!/usr/bin/env python3
"""Estimate Foodland neighbor island in-store prices from Instacart data.

Reads the Honolulu vs Neighbor Islands Instacart comparison CSV,
applies the per-item Honolulu markup ratio (instacart / in-store) to deflate
neighbor island Instacart prices back to estimated in-store prices,
and writes baseline entries for Maui, Hawaii, and Kauai.

Usage:
    python scripts/process_foodland_neighbor_islands.py
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent

# The 3 neighbor island counties to write entries for
NEIGHBOR_ISLAND_STORES = [
    {"store_id": "foodland-maui",   "county": "maui",   "geoid": "15009"},
    {"store_id": "foodland-hawaii", "county": "hawaii", "geoid": "15001"},
    {"store_id": "foodland-kauai",  "county": "kauai",  "geoid": "15007"},
]

PRODUCT_TO_SLOT = {
    "Hinode Calrose Rice, 20 lb":                    ("GRAIN-01", 20.0,   "lb"),
    "Cheerios, 12 oz":                               ("GRAIN-05", 12.0,   "oz"),
    "Barilla Spaghetti, 16 oz":                      ("GRAIN-07", 16.0,   "oz"),
    "Health and Harvest Bread, White, 22 oz":        ("GRAIN-03", 22.0,   "oz"),
    "Russet potatoes, 5 lb bag":                     ("VEG-01",   5.0,    "lb"),
    "Roma tomatoes, 1 lb":                           ("VEG-06",   1.0,    "lb"),
    "Del Monte Whole Kernel Corn, 15.25 oz":         ("VEG-08",   15.25,  "oz"),
    "Taylor Farms Baby Spinach, 6 oz":               ("VEG-04",   6.0,    "oz"),
    "Bananas, 1 lb":                                 ("FRUIT-01", 1.0,    "lb"),
    "Dole Pineapple Chunks in Juice, 20 oz":         ("FRUIT-05", 20.0,   "oz"),
    "Fuji Apple, 1 lb":                              ("FRUIT-03", 1.0,    "lb"),
    "Tropicana No Pulp Orange Juice, 89 oz":         ("FRUIT-08", 89.0,   "fl_oz"),
    "Sunhearth Milk, 1 gallon":                      ("DAIRY-01", 128.0,  "fl_oz"),
    "Land O'Lakes Salted Butter, 1 lb":              ("DAIRY-06", 16.0,   "oz"),
    "Farm Pack Mainland Eggs, Large, 12 each":       ("DAIRY-07", 12.0,   "ct"),
    "Kraft SIngles American Cheese Slices":          ("DAIRY-03", 12.0,   "oz"),
    "Ground beef 80/20, ~1 lb":                      ("MEAT-01",  1.0,    "lb"),
    "Boneless skinless chicken breast, 1 lb":        ("MEAT-04",  1.0,    "lb"),
    "SPAM Classic, 12 oz":                           ("MEAT-08",  12.0,   "oz"),
    "StarKist Chunk Light Tuna, 5 oz":               ("FISH-01",  5.0,    "oz"),
    "Jif Creamy Peanut Butter, 16 oz":               ("PROT-04",  16.0,   "oz"),
    "Wesson Vegetable Oil, 40 oz":                   ("FAT-01",   40.0,   "fl_oz"),
    "Folgers Classic Roast, 25.9 oz":                ("BEV-03",   25.9,   "oz"),
    "C&H Granulated Sugar, 4 lb":                    ("SWEET-01", 4.0,    "lb"),
    "Campbell's Chicken Noodle Soup, 10.75 oz":      ("PREP-01",  10.75,  "oz"),
    "Maruchan Ramen Soy Sauce, 2.25 oz":             ("PREP-03",  2.25,   "oz"),
}


def parse_price(s: str) -> float:
    return float(s.strip().replace("$", "").replace(",", ""))


def load_markup_ratios() -> dict[str, float]:
    """Load per-item Instacart markup ratios from calibration file."""
    cal_path = PROJECT_ROOT / "data/baseline/instacart_calibration.csv"
    ratios = {}
    with open(cal_path, newline="") as f:
        for row in csv.DictReader(f):
            ratios[row["slot_id"]] = float(row["markup_ratio"])
    return ratios


def main():
    raw_path = PROJECT_ROOT / "data/baseline/foodland_neighbor_islands_instacart_raw.csv"

    markup_ratios = load_markup_ratios()

    # Read the comparison CSV
    rows = []
    with open(raw_path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)

    # Compute estimated in-store prices for neighbor islands
    estimated = []
    print(f"{'Item':<45} {'NI Instacart':>12} {'÷ Markup':>8} {'= Est. In-Store':>15}")
    print("-" * 82)
    for row in rows:
        product = row["Product"].strip()
        ni_instacart = parse_price(row["Neighbor Islands"])

        slot_info = PRODUCT_TO_SLOT.get(product)
        if slot_info is None:
            print(f"  WARNING: no slot mapping for '{product}' — skipping")
            continue

        slot_id, size_qty, size_unit = slot_info
        markup = markup_ratios.get(slot_id, 1.0)
        est_instore = round(ni_instacart / markup, 2)
        per_unit = round(est_instore / size_qty, 4)

        print(f"{product:<45} ${ni_instacart:>10.2f} {markup:>8.3f}   ${est_instore:>12.2f}")

        estimated.append({
            "slot_id": slot_id,
            "product_name": product,
            "estimated_instore": est_instore,
            "size_qty": size_qty,
            "size_unit": size_unit,
            "per_unit": per_unit,
            "ni_instacart": ni_instacart,
            "markup_ratio": markup,
        })

    # Write one baseline CSV per neighbor island county
    fieldnames = ["slot_id", "product_name", "price", "size_qty", "size_unit",
                  "is_substitution", "substitution_note", "date"]

    for store in NEIGHBOR_ISLAND_STORES:
        out_path = PROJECT_ROOT / f"data/baseline/foodland_{store['county']}.csv"
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for item in sorted(estimated, key=lambda x: x["slot_id"]):
                writer.writerow({
                    "slot_id": item["slot_id"],
                    "product_name": item["product_name"],
                    "price": item["estimated_instore"],
                    "size_qty": item["size_qty"],
                    "size_unit": item["size_unit"],
                    "is_substitution": "false",
                    "substitution_note": "estimated from Instacart; deflated by Honolulu markup ratio",
                    "date": "2026-04-10",
                })
        print(f"\nSaved: {out_path.name}  ({len(estimated)} items)")

    # Summary
    print(f"\n{'='*50}")
    total_honolulu = sum(  # from existing baseline
        parse_price(row["Honolulu"]) / markup_ratios.get(
            PRODUCT_TO_SLOT.get(row["Product"].strip(), ("", 0, ""))[0], 1.0
        )
        for row in rows if row["Product"].strip() in PRODUCT_TO_SLOT
    )
    total_ni = sum(item["estimated_instore"] for item in estimated)
    diff = total_ni - total_honolulu
    pct = diff / total_honolulu * 100
    print(f"Estimated basket total — Honolulu in-store: ${sum(parse_price(row['Honolulu']) / markup_ratios.get(PRODUCT_TO_SLOT.get(row['Product'].strip(), ('',0,''))[0], 1.0) for row in rows if row['Product'].strip() in PRODUCT_TO_SLOT):.2f}")
    print(f"Estimated basket total — Neighbor Islands:  ${total_ni:.2f}")
    print(f"Neighbor island premium: +${diff:.2f} (+{pct:.1f}%)")


if __name__ == "__main__":
    main()
