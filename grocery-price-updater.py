#!/usr/bin/env python3
"""
grocery-price-updater.py
------------------------
Reads the latest Hawaiʻi Grocery Price Tracker outputs and patches the
`groceryData` block in both squarespace-single-file.html and index.html.

Source CSVs (in-repo grocery pipeline):
    pipelines/grocery/data/output/household_estimates.csv
    pipelines/grocery/data/output/county_comparison.csv

Fields written per county:
    basketPretax, basketWithTax, monthlyFamily4, groceryIdx,
    groceryShareOfIncome, weeklyPerCap, lastUpdated,
    household{singleFemale,singleMale,singleParent1Child,family4},
    categories{grains,vegetables,fruits,...},
    topItems[{name,price,vsHnl}]

Patch strategy: replace everything between
    /* GROCERY_DATA_START */  ...  /* GROCERY_DATA_END */
markers in the HTML.

Run:
    python3 grocery-price-updater.py
    python3 grocery-price-updater.py --dry-run
    python3 grocery-price-updater.py --file index.html
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

# -----------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
GROCERY_PIPELINE_ROOT = PROJECT_ROOT / "pipelines" / "grocery"
HOUSEHOLD_CSV = GROCERY_PIPELINE_ROOT / "data" / "output" / "household_estimates.csv"
COUNTY_CSV    = GROCERY_PIPELINE_ROOT / "data" / "output" / "county_comparison.csv"

DEFAULT_FILES = [
    PROJECT_ROOT / "squarespace-single-file.html",
    PROJECT_ROOT / "index.html",
]

# HUD FY 2025 MFI (4-person) — kept in sync with countyData in HTML
INCOME_BY_COUNTY = {
    "State":    123000,
    "Honolulu": 129300,
    "Maui":     110900,
    "Hawaii":    98800,
    "Kauai":    132900,
}

# Population weights for computing statewide averages (DBEDT 2024 estimates)
POP = {
    "Honolulu": 1016000,
    "Maui":     167000,
    "Hawaii":   201000,
    "Kauai":     73000,
}

HH_TYPE_MAP = {
    "single_adult_female":     "singleFemale",
    "single_adult_male":       "singleMale",
    "single_parent_one_child": "singleParent1Child",
    "two_adults_two_children": "family4",
}

# slot_id prefix → category key in index_weights.json
CAT_PREFIX = {
    "GRAIN": "grains",
    "VEG":   "vegetables",
    "FRUIT": "fruits",
    "DAIRY": "dairy",
    "MEAT":  "protein_meat",
    "FISH":  "protein_seafood",
    "PROT":  "protein_other",
    "FAT":   "fats_oils",
    "BEV":   "beverages",
    "COND":  "condiments",
    "SWEET": "sugars_sweets",
    "SNACK": "snacks",
    "PREP":  "prepared",
}
ALL_CATS = ["grains", "vegetables", "fruits", "dairy", "protein_meat",
            "protein_seafood", "protein_other", "fats_oils", "beverages",
            "condiments", "sugars_sweets", "snacks", "prepared"]

COUNTY_CSV_TO_HTML = {
    "honolulu": "Honolulu",
    "maui":     "Maui",
    "hawaii":   "Hawaii",
    "kauai":    "Kauai",
}

# -----------------------------------------------------------------
# Data loaders
# -----------------------------------------------------------------
def load_household_estimates() -> dict:
    """
    Returns: { county_html_key: { hh_html_key: weekly_post_tax, ... }, ... }
    Also includes 'basketPretax', 'basketWithTax', 'family4' from the
    two_adults_two_children row (= full basket).
    """
    out: dict = {c: {} for c in COUNTY_CSV_TO_HTML.values()}
    pretax = {c: 0.0 for c in COUNTY_CSV_TO_HTML.values()}
    withtax = {c: 0.0 for c in COUNTY_CSV_TO_HTML.values()}
    last_date = None

    with HOUSEHOLD_CSV.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            hh_csv = row["household_type"]
            county_csv = row["county"].lower()
            if county_csv not in COUNTY_CSV_TO_HTML:
                continue
            cty = COUNTY_CSV_TO_HTML[county_csv]
            hh_key = HH_TYPE_MAP.get(hh_csv)
            if not hh_key:
                continue
            out[cty][hh_key] = float(row["household_cost_with_tax"])
            if hh_csv == "two_adults_two_children":
                pretax[cty] = float(row["basket_total_pretax"])
                withtax[cty] = float(row["household_cost_with_tax"])
            last_date = row.get("date") or last_date

    return out, pretax, withtax, last_date


def load_county_items() -> tuple[list[dict], dict]:
    """
    Returns (item_rows, category_totals_by_county)
      item_rows: [{slot_id, item, honolulu, maui, hawaii, kauai, unit}, ...]
      category_totals: { 'Honolulu': {'grains': 36.66, ...}, ... }
    """
    rows: list[dict] = []
    cat_totals: dict = {c: {k: 0.0 for k in ALL_CATS} for c in COUNTY_CSV_TO_HTML.values()}

    with COUNTY_CSV.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            slot = (r.get("slot_id") or "").strip()
            if not slot or "SUBTOTAL" in (r.get("item") or "") or "TOTAL" in (r.get("item") or ""):
                continue
            rows.append(r)
            prefix = slot.split("-")[0]
            cat = CAT_PREFIX.get(prefix)
            if not cat:
                continue
            for county_csv, county_html in COUNTY_CSV_TO_HTML.items():
                try:
                    cat_totals[county_html][cat] += float(r[county_csv])
                except (KeyError, ValueError):
                    pass
    return rows, cat_totals


# -----------------------------------------------------------------
# Computation
# -----------------------------------------------------------------
def compute_statewide(pretax: dict, withtax: dict, hh_by_cty: dict,
                      cat_totals: dict, items: list[dict]) -> tuple:
    """Population-weight the four county figures into a statewide figure."""
    total_pop = sum(POP.values())
    w = {c: POP[c] / total_pop for c in POP}

    state_pretax  = sum(pretax[c]  * w[c] for c in POP)
    state_withtax = sum(withtax[c] * w[c] for c in POP)

    state_hh = {}
    for hh_key in ("singleFemale", "singleMale", "singleParent1Child", "family4"):
        state_hh[hh_key] = sum(hh_by_cty[c].get(hh_key, 0.0) * w[c] for c in POP)

    state_cats = {k: sum(cat_totals[c][k] * w[c] for c in POP) for k in ALL_CATS}

    # State top-items use population-weighted per-item prices
    state_items = []
    for r in items:
        try:
            price = sum(float(r[csvk]) * w[htmlk] for csvk, htmlk in COUNTY_CSV_TO_HTML.items())
        except (KeyError, ValueError):
            continue
        state_items.append({**r, "_weighted_price": price})
    return state_pretax, state_withtax, state_hh, state_cats, state_items


def build_top_items(items: list[dict], county_csv_key: str | None,
                    state_items: list[dict] | None, honolulu_prices: dict,
                    limit: int = 8) -> list[dict]:
    """
    Return top-N items by price for this county, each with its price vs. Honolulu.
    honolulu_prices: { slot_id: price }
    """
    scored = []
    for r in items:
        slot = r["slot_id"]
        if county_csv_key:
            try:
                price = float(r[county_csv_key])
            except (KeyError, ValueError):
                continue
        else:
            price = r.get("_weighted_price")
            if price is None:
                continue
        hnl = honolulu_prices.get(slot, 0.0)
        vs_hnl = (price / hnl) if hnl else 1.0
        # trim item name
        name = r["item"]
        if "(" in name:
            name = name.split("(")[0].strip().rstrip(",")
        scored.append({"name": name, "price": round(price, 2), "vsHnl": round(vs_hnl, 3)})
    scored.sort(key=lambda x: x["price"], reverse=True)
    return scored[:limit]


def build_grocery_data() -> dict:
    hh_by_cty, pretax, withtax, last_date = load_household_estimates()
    items, cat_totals = load_county_items()
    honolulu_prices = {r["slot_id"]: float(r["honolulu"]) for r in items if r.get("honolulu")}

    state_pretax, state_withtax, state_hh, state_cats, state_items = \
        compute_statewide(pretax, withtax, hh_by_cty, cat_totals, items)

    out: dict = {}
    hnl_withtax = withtax["Honolulu"] or 1.0

    for cty in ("State", "Honolulu", "Maui", "Hawaii", "Kauai"):
        if cty == "State":
            bt_pre   = state_pretax
            bt_wtax  = state_withtax
            hh       = state_hh
            cats     = state_cats
            top      = build_top_items(state_items, None, state_items, honolulu_prices)
        else:
            bt_pre   = pretax[cty]
            bt_wtax  = withtax[cty]
            hh       = hh_by_cty[cty]
            cats     = cat_totals[cty]
            top      = build_top_items(items, cty.lower(), None, honolulu_prices)

        monthly_family4 = round(hh.get("family4", bt_wtax) * 4.33)
        income = INCOME_BY_COUNTY[cty]
        share = (monthly_family4 * 12) / income if income else 0.0
        grocery_idx = (bt_wtax / hnl_withtax * 100) if hnl_withtax else 100.0
        weekly_per_cap = round(hh.get("family4", bt_wtax) / 4)

        out[cty] = {
            "basketPretax":        round(bt_pre, 2),
            "basketWithTax":       round(bt_wtax, 2),
            "monthlyFamily4":      monthly_family4,
            "groceryIdx":          round(grocery_idx, 1),
            "groceryShareOfIncome": round(share, 4),
            "weeklyPerCap":        weekly_per_cap,
            "lastUpdated":         last_date or "",
            "household":           {k: round(v, 2) for k, v in hh.items()},
            "categories":          {k: round(v, 2) for k, v in cats.items()},
            "topItems":            top,
        }
    return out


# -----------------------------------------------------------------
# HTML rendering & patching
# -----------------------------------------------------------------
def _js_lit(v) -> str:
    """Render a Python value as a compact JS literal."""
    if isinstance(v, bool):   return "true" if v else "false"
    if isinstance(v, (int,)): return str(v)
    if isinstance(v, float):  return repr(round(v, 4))
    if isinstance(v, str):    return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[ " + ", ".join(_js_lit(x) for x in v) + " ]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k}:{_js_lit(val)}" for k, val in v.items()) + " }"
    raise TypeError(f"unsupported type {type(v)}")


def render_grocery_data_block(data: dict) -> str:
    """
    Render as:
      /* GROCERY_DATA_START */
      const groceryData = {
        State:    {...},
        Honolulu: {...},
        ...
      };
      /* GROCERY_DATA_END */
    """
    lines = ["/* GROCERY_DATA_START */", "const groceryData = {"]
    order = ("State", "Honolulu", "Maui", "Hawaii", "Kauai")
    pad = max(len(c) for c in order)
    for c in order:
        key = (c + ":").ljust(pad + 2)
        lines.append(f"  {key}{_js_lit(data[c])},")
    lines.append("};")
    lines.append("/* GROCERY_DATA_END */")
    return "\n".join(lines)


PATCH_RE = re.compile(
    r"/\* GROCERY_DATA_START \*/.*?/\* GROCERY_DATA_END \*/",
    flags=re.DOTALL,
)


def patch_html(html: str, new_block: str) -> tuple[str, bool]:
    if not PATCH_RE.search(html):
        return html, False
    return PATCH_RE.sub(lambda m: new_block, html, count=1), True


# -----------------------------------------------------------------
# CLI
# -----------------------------------------------------------------
def main() -> int:
    dry_run = "--dry-run" in sys.argv
    files: list[Path]
    if "--file" in sys.argv:
        idx = sys.argv.index("--file") + 1
        files = [Path(sys.argv[idx])]
    else:
        files = DEFAULT_FILES

    if not HOUSEHOLD_CSV.exists():
        print(f"ERROR: {HOUSEHOLD_CSV} not found", file=sys.stderr)
        return 1
    if not COUNTY_CSV.exists():
        print(f"ERROR: {COUNTY_CSV} not found", file=sys.stderr)
        return 1

    print(f"Loading grocery data from {GROCERY_PIPELINE_ROOT}/data/output/")
    data = build_grocery_data()
    for c in ("State", "Honolulu", "Maui", "Hawaii", "Kauai"):
        g = data[c]
        print(f"  {c:9s} basket=${g['basketWithTax']:6.2f}/wk  "
              f"family4=${g['monthlyFamily4']}/mo  "
              f"idx={g['groceryIdx']:5.1f}  "
              f"share={g['groceryShareOfIncome']*100:4.1f}%  "
              f"items={len(g['topItems'])}")

    new_block = render_grocery_data_block(data)

    if dry_run:
        print("\n--- DRY RUN: block to be written ---")
        print(new_block[:2000])
        print("...")
        return 0

    for path in files:
        if not path.exists():
            print(f"  skipping {path} (not found)")
            continue
        html = path.read_text()
        new_html, ok = patch_html(html, new_block)
        if not ok:
            print(f"  WARNING: no GROCERY_DATA markers found in {path}")
            continue
        if new_html == html:
            print(f"  {path.name}: no change")
        else:
            path.write_text(new_html)
            print(f"  {path.name}: updated ✓")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
