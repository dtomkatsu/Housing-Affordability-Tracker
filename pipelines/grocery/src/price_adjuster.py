"""Apply CPI-based adjustments to baseline prices."""

import csv
from datetime import date
from pathlib import Path

from .cpi_fetcher import find_nearest_periods, get_cpi_value, date_to_bls_period
from .models import AdjustedPrice, BaselinePrice, BasketConfig, CPIConfig


def load_baseline(path: Path) -> list[BaselinePrice]:
    """Load consolidated baseline CSV into BaselinePrice objects."""
    prices = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            prices.append(BaselinePrice(
                slot_id=row["slot_id"],
                chain=row["chain"],
                store_id=row["store_id"],
                county=row["county"],
                geoid=row["geoid"],
                date=row["date"],
                product_name=row["product_name"],
                price=float(row["price"]),
                size_qty=float(row["size_qty"]),
                size_unit=row["size_unit"],
                per_unit_price=float(row["per_unit_price"]),
                is_substitution=row["is_substitution"].lower() in ("true", "1"),
                substitution_note=row.get("substitution_note") or None,
            ))
    return prices


def compute_cpi_ratio(
    cpi_data: dict,
    series_id: str,
    baseline_date: date,
    target_date: date,
) -> float:
    """Compute CPI ratio (target / baseline) for a series, with interpolation.

    Returns 1.0 if data is unavailable (no adjustment).
    """
    # Get baseline CPI value
    base_year, base_period = date_to_bls_period(baseline_date)
    before, after = find_nearest_periods(cpi_data, series_id, base_year, baseline_date.month)

    if before is None:
        return 1.0

    if after is None:
        baseline_cpi = before["value"]
    else:
        # Interpolate between bracketing periods
        baseline_cpi = _interpolate(before, after, baseline_date)

    # Get target CPI value
    t_before, t_after = find_nearest_periods(cpi_data, series_id, target_date.year, target_date.month)

    if t_before is None:
        return 1.0

    if t_after is None:
        target_cpi = t_before["value"]
    else:
        target_cpi = _interpolate(t_before, t_after, target_date)

    if baseline_cpi == 0:
        return 1.0

    return target_cpi / baseline_cpi


def _interpolate(before: dict, after: dict, target: date) -> float:
    """Linear interpolation between two CPI data points."""
    if before == after:
        return before["value"]

    b_month = before["year"] * 12 + int(before["period"][1:])
    a_month = after["year"] * 12 + int(after["period"][1:])
    t_month = target.year * 12 + target.month

    span = a_month - b_month
    if span == 0:
        return before["value"]

    fraction = (t_month - b_month) / span
    return before["value"] + fraction * (after["value"] - before["value"])


def adjust_prices(
    baseline_prices: list[BaselinePrice],
    cpi_data: dict,
    cpi_config: CPIConfig,
    basket: BasketConfig,
    target_date: date,
) -> list[AdjustedPrice]:
    """Adjust all baseline prices to a target date using CPI ratios."""
    adjusted = []

    # Pre-compute CPI ratios per category
    ratios = {}
    for bp in baseline_prices:
        item = basket.get_item(bp.slot_id)
        if item is None:
            continue

        cpi_cat = item["cpi_category"]
        if cpi_cat not in ratios:
            cat_config = cpi_config.categories.get(cpi_cat)
            if cat_config is None:
                ratios[cpi_cat] = 1.0
                continue
            series_id = cat_config["series_id"]
            base_date = date.fromisoformat(bp.date)
            ratios[cpi_cat] = compute_cpi_ratio(cpi_data, series_id, base_date, target_date)

    # Apply ratios
    for bp in baseline_prices:
        item = basket.get_item(bp.slot_id)
        if item is None:
            continue

        cpi_cat = item["cpi_category"]
        ratio = ratios.get(cpi_cat, 1.0)
        adj_price = round(bp.price * ratio, 2)
        adj_per_unit = round(bp.per_unit_price * ratio, 4)

        adjusted.append(AdjustedPrice(
            slot_id=bp.slot_id,
            chain=bp.chain,
            store_id=bp.store_id,
            county=bp.county,
            geoid=bp.geoid,
            baseline_date=bp.date,
            adjusted_date=target_date.isoformat(),
            baseline_price=bp.price,
            adjusted_price=adj_price,
            per_unit_price=adj_per_unit,
            cpi_category=cpi_cat,
            cpi_ratio=round(ratio, 6),
        ))

    return adjusted
