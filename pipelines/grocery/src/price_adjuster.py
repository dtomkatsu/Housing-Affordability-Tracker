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


def _latest_observed_point(points: list[dict]) -> dict | None:
    """Return the most recent (year, period) observation for a BLS series."""
    if not points:
        return None
    return max(points, key=lambda p: (p["year"], int(p["period"][1:])))


def _project_forward(points: list[dict], target_date: date) -> float:
    """Extrapolate a CPI index value forward past the last observed bimonthly point.

    Uses the *compound* monthly rate derived from the last two observed Honolulu
    bimonthly points (typically 2 months apart). We then multiply the latest
    observed index by (1 + monthly_rate)**(months_beyond) — a linear-trend
    projection that collapses to flat when the series is flat, but honestly
    carries momentum when prices are still moving.

    Caller must ensure *target_date* is strictly past the latest observed
    point. Returns the projected index value.
    """
    if not points:
        raise ValueError("cannot project forward from empty series")

    ordered = sorted(points, key=lambda p: (p["year"], int(p["period"][1:])))
    latest = ordered[-1]
    latest_year, latest_month = latest["year"], int(latest["period"][1:])
    months_beyond = (target_date.year - latest_year) * 12 + (target_date.month - latest_month)

    if months_beyond <= 0 or len(ordered) < 2:
        # Defensive: caller should have screened exact / past-bracketed cases.
        return float(latest["value"])

    prev = ordered[-2]
    prev_year, prev_month = prev["year"], int(prev["period"][1:])
    months_between = (latest_year - prev_year) * 12 + (latest_month - prev_month)
    if months_between <= 0 or prev["value"] <= 0:
        return float(latest["value"])

    # Compound growth per month, then project forward. Cap the per-period
    # rate at ±0.0189/month (~±25% annualized) to stop a single noisy bimonthly
    # print from compounding into an unrealistic extrapolation.
    monthly_rate = (latest["value"] / prev["value"]) ** (1.0 / months_between) - 1.0
    monthly_rate = max(min(monthly_rate, 0.0189), -0.0189)
    return float(latest["value"] * (1.0 + monthly_rate) ** months_beyond)


def _value_at(
    cpi_data: dict, series_id: str, points: list[dict], latest: dict | None,
    target_date: date,
) -> tuple[float | None, str]:
    """Resolve a CPI index value at *target_date* and label which method was used.

    Returns (value, method) where method is one of:
        'exact'        — target matches an observed bimonthly period
        'interpolated' — target is strictly between two observed points
        'projected'    — target is past the latest observation (forward extrapolation)
        'unavailable'  — no usable data

    Disambiguates the find_nearest_periods "(point, None)" return: that shape
    can mean either "exact match" OR "past the last observed point", and the
    previous code conflated them into a silent flat extrapolation.
    """
    if latest is None:
        return None, "unavailable"

    before, after = find_nearest_periods(cpi_data, series_id, target_date.year, target_date.month)
    if before is None:
        return None, "unavailable"

    latest_year, latest_month = latest["year"], int(latest["period"][1:])
    beyond_latest = (target_date.year, target_date.month) > (latest_year, latest_month)

    if after is None:
        if beyond_latest:
            return _project_forward(points, target_date), "projected"
        # Exact match on an observed period (the "before" point IS the target).
        return float(before["value"]), "exact"

    return _interpolate(before, after, target_date), "interpolated"


def compute_cpi_ratio(
    cpi_data: dict,
    series_id: str,
    baseline_date: date,
    target_date: date,
) -> dict:
    """Compute CPI ratio (target / baseline) for a series.

    Returns a dict:
        ratio            float    — target_cpi / baseline_cpi (1.0 if unavailable)
        is_projected     bool     — target is beyond the last observed bimonthly point
        method           str      — 'exact' | 'interpolated' | 'projected' | 'unavailable'
        latest_observed  str|None — ISO period "YYYY-MM" of the latest observation
        target_period    str      — ISO period of the target date

    The previous version silently flat-lined when *target_date* was past the
    last observed point. That hid a "no change since last observation" assumption
    from the caller. We now detect the edge explicitly, linear-trend project the
    index forward, and flag it so downstream UI can surface a `proj.` tag.
    """
    target_iso = f"{target_date.year}-{target_date.month:02d}"
    points = cpi_data.get(series_id, [])
    latest = _latest_observed_point(points)
    latest_iso = (
        f"{latest['year']}-{int(latest['period'][1:]):02d}"
        if latest is not None else None
    )

    result = {
        "ratio":           1.0,
        "is_projected":    False,
        "method":          "unavailable",
        "latest_observed": latest_iso,
        "target_period":   target_iso,
    }

    baseline_cpi, _bmethod = _value_at(cpi_data, series_id, points, latest, baseline_date)
    target_cpi,   tmethod  = _value_at(cpi_data, series_id, points, latest, target_date)

    if baseline_cpi is None or target_cpi is None or baseline_cpi == 0:
        return result

    result["ratio"]        = target_cpi / baseline_cpi
    result["method"]       = tmethod
    result["is_projected"] = (tmethod == "projected")
    return result


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
) -> tuple[list[AdjustedPrice], dict]:
    """Adjust all baseline prices to a target date using CPI ratios.

    Returns (adjusted_prices, ratio_info) where ratio_info is
        { cpi_category: { ratio, is_projected, method, latest_observed, target_period } }
    — one entry per category actually used. Callers use it to surface
    projection state (e.g. the "proj." tag in the dashboard).
    """
    adjusted = []

    # Pre-compute CPI ratios per category
    ratios: dict[str, dict] = {}
    for bp in baseline_prices:
        item = basket.get_item(bp.slot_id)
        if item is None:
            continue

        cpi_cat = item["cpi_category"]
        if cpi_cat not in ratios:
            cat_config = cpi_config.categories.get(cpi_cat)
            if cat_config is None:
                ratios[cpi_cat] = {
                    "ratio": 1.0, "is_projected": False, "method": "unavailable",
                    "latest_observed": None,
                    "target_period": f"{target_date.year}-{target_date.month:02d}",
                }
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
        info = ratios.get(cpi_cat) or {"ratio": 1.0}
        ratio = info["ratio"]
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

    return adjusted, ratios
