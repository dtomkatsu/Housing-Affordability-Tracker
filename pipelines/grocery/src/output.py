"""Generate tables (CSV) and charts from adjusted prices and household estimates."""

import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .models import AdjustedPrice, BasketConfig, HouseholdEstimate, StoreWeightsConfig


OUTPUT_DIR = Path(__file__).parent.parent / "data" / "output"


def compute_weighted_county_prices(
    adjusted_prices: list[AdjustedPrice],
    store_weights: StoreWeightsConfig | None = None,
) -> list[AdjustedPrice]:
    """Collapse per-store prices into weighted per-county averages.

    For each (slot_id, county), averages across chains using market share
    weights. If store_weights is None, uses equal weighting (backward
    compatible).

    Returns a list with one AdjustedPrice per (slot_id, county) where
    chain="_weighted_avg" and store_id="_weighted_avg".
    """
    # Group: (slot_id, county) -> {chain -> [prices]}
    grouped: dict[tuple[str, str], dict[str, list[AdjustedPrice]]] = {}
    for ap in adjusted_prices:
        key = (ap.slot_id, ap.county)
        grouped.setdefault(key, {}).setdefault(ap.chain, []).append(ap)

    collapsed: list[AdjustedPrice] = []
    for (slot_id, county), chain_data in grouped.items():
        # Step 1: Average within each chain (handles multiple stores per chain)
        chain_avg: dict[str, float] = {}
        for chain, ap_list in chain_data.items():
            chain_avg[chain] = sum(ap.adjusted_price for ap in ap_list) / len(ap_list)

        # Step 2: Weighted average across chains
        if store_weights is None or len(chain_avg) == 1:
            # No weights or single chain — simple mean
            avg_price = sum(chain_avg.values()) / len(chain_avg)
        else:
            # Use effective_weights() which absorbs proxy chain weights into
            # covered chains before renormalizing. Falls back to equal weights
            # if no county data exists.
            eff_weights = store_weights.effective_weights(county, list(chain_avg.keys()))
            avg_price = sum(chain_avg[c] * eff_weights[c] for c in chain_avg)

        # Pick a representative AdjustedPrice as template for metadata
        template = next(iter(next(iter(chain_data.values()))))
        collapsed.append(AdjustedPrice(
            slot_id=slot_id,
            chain="_weighted_avg",
            store_id="_weighted_avg",
            county=county,
            geoid=template.geoid,
            baseline_date=template.baseline_date,
            adjusted_date=template.adjusted_date,
            baseline_price=template.baseline_price,
            adjusted_price=round(avg_price, 2),
            per_unit_price=round(avg_price, 2),
            cpi_category=template.cpi_category,
            cpi_ratio=template.cpi_ratio,
        ))

    return collapsed


def generate_county_comparison_csv(
    adjusted_prices: list[AdjustedPrice],
    basket: BasketConfig,
    output_path: Path | None = None,
) -> Path:
    """Generate county price comparison table as CSV.

    Expects pre-collapsed prices (one per slot_id per county) from
    compute_weighted_county_prices(). Also works with raw per-store
    prices by averaging them (backward compatible).
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "county_comparison.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Group prices: slot_id -> county -> price
    price_map: dict[str, dict[str, float]] = {}
    counties = set()
    for ap in adjusted_prices:
        counties.add(ap.county)
        if ap.slot_id not in price_map:
            price_map[ap.slot_id] = {}
        # Average across stores in same county
        key = ap.county
        if key not in price_map[ap.slot_id]:
            price_map[ap.slot_id][key] = []
        price_map[ap.slot_id][key].append(ap.adjusted_price)

    counties_sorted = sorted(counties)

    # Average prices per county
    avg_map: dict[str, dict[str, float]] = {}
    for slot_id, county_prices in price_map.items():
        avg_map[slot_id] = {}
        for county, prices in county_prices.items():
            avg_map[slot_id][county] = round(sum(prices) / len(prices), 2)

    # Write CSV
    fieldnames = ["slot_id", "item"] + counties_sorted + ["unit"]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        subtotals = {c: 0.0 for c in counties_sorted}
        for item in basket.items:
            slot_id = item["slot_id"]
            row = {
                "slot_id": slot_id,
                "item": item["description"],
                "unit": f"/{item['norm_unit']}",
            }
            for county in counties_sorted:
                price = avg_map.get(slot_id, {}).get(county)
                row[county] = f"{price:.2f}" if price else ""
                if price:
                    subtotals[county] += price
            writer.writerow(row)

        # Subtotal row (pre-tax)
        subtotal_row = {"slot_id": "", "item": "SUBTOTAL (pre-tax)", "unit": ""}
        for county in counties_sorted:
            subtotal_row[county] = f"{subtotals[county]:.2f}"
        writer.writerow(subtotal_row)

        # Total row (with 4.5% GET tax)
        total_row = {"slot_id": "", "item": "TOTAL (with 4.5% GET tax)", "unit": ""}
        for county in counties_sorted:
            total_row[county] = f"{subtotals[county] * 1.045:.2f}"
        writer.writerow(total_row)

    return output_path


def generate_chain_comparison_csv(
    adjusted_prices: list[AdjustedPrice],
    basket: BasketConfig,
    output_path: Path | None = None,
) -> Path:
    """Generate per-chain, per-county price table.

    Shows each chain's prices separately so inter-island spreads
    per chain are visible (e.g. Foodland's spread vs Safeway's spread).
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "chain_comparison.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect unique (chain, county) pairs
    chain_counties: dict[str, set[str]] = {}
    for ap in adjusted_prices:
        chain_counties.setdefault(ap.chain, set()).add(ap.county)

    # Build price lookup: (slot_id, chain, county) -> price
    price_lookup: dict[tuple[str, str, str], float] = {}
    for ap in adjusted_prices:
        key = (ap.slot_id, ap.chain, ap.county)
        price_lookup[key] = ap.adjusted_price

    chains_sorted = sorted(chain_counties.keys())
    counties_sorted = sorted(set(c for cs in chain_counties.values() for c in cs))

    # Column headers: chain_county pairs
    col_keys = []
    col_headers = []
    for chain in chains_sorted:
        for county in counties_sorted:
            if county in chain_counties[chain]:
                col_keys.append((chain, county))
                col_headers.append(f"{chain}_{county}")

    fieldnames = ["slot_id", "item"] + col_headers + ["unit"]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        totals = {h: 0.0 for h in col_headers}
        for item in basket.items:
            slot_id = item["slot_id"]
            row = {
                "slot_id": slot_id,
                "item": item["description"],
                "unit": f"/{item['norm_unit']}",
            }
            for (chain, county), header in zip(col_keys, col_headers):
                price = price_lookup.get((slot_id, chain, county))
                row[header] = f"{price:.2f}" if price else ""
                if price:
                    totals[header] += price
            writer.writerow(row)

        total_row = {"slot_id": "", "item": "BASKET TOTAL", "unit": ""}
        for header in col_headers:
            total_row[header] = f"{totals[header]:.2f}"
        writer.writerow(total_row)

    return output_path


def generate_household_csv(
    estimates: list[HouseholdEstimate],
    output_path: Path | None = None,
    get_tax_rate: float = 0.045,
) -> Path:
    """Generate household cost estimates table as CSV (with GET tax).

    Includes both pre-tax and post-tax (effective) costs.
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "household_estimates.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "household_type", "household_label", "county", "geoid",
        "date", "basket_total_pretax", "household_cost_pretax", "household_cost_with_tax",
        "effective_factor", "get_tax_rate",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for est in sorted(estimates, key=lambda e: (e.household_type, e.county)):
            cost_with_tax = round(est.household_cost * (1 + get_tax_rate), 2)
            writer.writerow({
                "household_type": est.household_type,
                "household_label": est.household_label,
                "county": est.county,
                "geoid": est.geoid,
                "date": est.date,
                "basket_total_pretax": est.basket_total,
                "household_cost_pretax": est.household_cost,
                "household_cost_with_tax": cost_with_tax,
                "effective_factor": est.effective_factor,
                "get_tax_rate": get_tax_rate,
            })

    return output_path


def generate_county_bar_chart(
    adjusted_prices: list[AdjustedPrice],
    output_path: Path | None = None,
    get_tax_rate: float = 0.045,
) -> Path:
    """Generate bar chart comparing basket totals by county.

    Args:
        adjusted_prices: List of adjusted prices (collapsed to one per county).
        output_path: Output PNG path.
        get_tax_rate: Hawaii General Excise Tax rate (default 4.5% statewide).
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "county_comparison.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Sum basket total per county, apply GET tax
    county_totals: dict[str, float] = {}
    for ap in adjusted_prices:
        subtotal = county_totals.get(ap.county, 0.0) + ap.adjusted_price
        # Apply GET tax to final total
        county_totals[ap.county] = subtotal

    # Apply GET tax to final totals
    county_totals = {c: round(t * (1 + get_tax_rate), 2) for c, t in county_totals.items()}

    counties = sorted(county_totals.keys())
    totals = [county_totals[c] for c in counties]
    labels = [c.title() for c in counties]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(labels, totals, color=["#2196F3", "#4CAF50", "#FF9800", "#9C27B0"])
    ax.set_ylabel("Basket Total ($)")
    ax.set_title("26-Item Grocery Basket Cost by County")
    ax.set_ylim(0, max(totals) * 1.15)

    for bar, total in zip(bars, totals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                f"${total:.2f}", ha="center", va="bottom", fontweight="bold")

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()

    return output_path


def generate_household_bar_chart(
    estimates: list[HouseholdEstimate],
    output_path: Path | None = None,
    get_tax_rate: float = 0.045,
) -> Path:
    """Generate grouped bar chart of household costs by county (with GET tax).

    Args:
        estimates: Household cost estimates.
        output_path: Output PNG path.
        get_tax_rate: Hawaii General Excise Tax rate (default 4.5% statewide).
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "household_costs.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Organize data
    hh_types = sorted(set(e.household_type for e in estimates))
    counties = sorted(set(e.county for e in estimates))

    # Build lookup: (hh_type, county) -> cost (with GET tax)
    cost_map = {
        (e.household_type, e.county): round(e.household_cost * (1 + get_tax_rate), 2)
        for e in estimates
    }

    fig, ax = plt.subplots(figsize=(10, 6))
    x = range(len(counties))
    width = 0.8 / len(hh_types)
    colors = ["#2196F3", "#4CAF50", "#FF9800", "#9C27B0"]

    for i, hh_type in enumerate(hh_types):
        offsets = [xi + i * width - 0.4 + width / 2 for xi in x]
        values = [cost_map.get((hh_type, c), 0) for c in counties]
        label_text = next(
            (e.household_label for e in estimates if e.household_type == hh_type),
            hh_type
        )
        ax.bar(offsets, values, width, label=label_text, color=colors[i % len(colors)])

    ax.set_xticks(range(len(counties)))
    ax.set_xticklabels([c.title() for c in counties])
    ax.set_ylabel("Monthly Grocery Cost ($)")
    ax.set_title(f"Estimated Monthly Grocery Cost by Household Type and County (with {get_tax_rate*100:.1f}% GET tax)")
    ax.legend(loc="upper left", fontsize=8)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()

    return output_path
