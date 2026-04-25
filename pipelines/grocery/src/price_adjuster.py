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


# Per-month projection cap: bounds noisy bimonthly print from compounding
# into an unrealistic extrapolation. (1+0.0189)^12 ≈ 1.252 → ±~25%/yr.
PROJ_MONTHLY_CAP = 0.0189

# Damping factor (Gardner & McKenzie 1985, used in Holt damped-trend).
# Each successive month of forecast carries (PROJ_DAMPING)^h of the trend,
# so the projection asymptotes rather than compounding indefinitely. φ=0.92
# means by month 6 we apply ~61% of the latest trend; by month 12, ~37%.
# This guards against the most pernicious failure mode of trend extrapolation:
# treating a transient bimonthly spike as a permanent slope.
PROJ_DAMPING = 0.92


def _smoothed_monthly_rate(ordered: list[dict]) -> float | None:
    """Recency-weighted geometric mean of pairwise monthly growth rates.

    For a series with N≥2 observations, computes the per-month compound rate
    between each consecutive pair, then blends them with exponential recency
    weights (most-recent rate weight 1.0, prior 0.5, prior-prior 0.25, …).

    With exactly 2 points this collapses to the single pairwise rate — the
    previous behaviour. With 3+ points the smoothing absorbs single-period
    noise: a one-print spike that doesn't repeat gets diluted by the prior
    trend, mirroring how Holt's linear-trend smoother (β<1) carries momentum
    rather than slavishly chasing the latest delta.

    Returns None if no usable pair exists (e.g. all zeros or non-monotonic
    timestamps); caller should fall back to the latest observed value.
    """
    pairs: list[tuple[float, float]] = []  # (monthly_rate, weight)
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        curr = ordered[i]
        prev_y, prev_m = prev["year"], int(prev["period"][1:])
        curr_y, curr_m = curr["year"], int(curr["period"][1:])
        months_between = (curr_y - prev_y) * 12 + (curr_m - prev_m)
        if months_between <= 0 or prev["value"] <= 0:
            continue
        monthly_rate = (curr["value"] / prev["value"]) ** (1.0 / months_between) - 1.0
        # Most recent pair: weight 1.0; each step back halves the weight.
        # Sum of weights for n pairs: 1 + 0.5 + 0.25 + … converges to 2.
        weight = 0.5 ** (len(ordered) - 1 - i)
        pairs.append((monthly_rate, weight))

    if not pairs:
        return None
    return sum(r * w for r, w in pairs) / sum(w for _, w in pairs)


def _damped_compound_factor(monthly_rate: float, months_beyond: int) -> float:
    """Compound growth over `months_beyond` with Gardner-McKenzie damping.

    Each successive month applies PROJ_DAMPING^(h-1) of the trend, so the
    projection slope decays rather than compounding flat. Reduces to standard
    compounding when PROJ_DAMPING == 1.0, and to flat when monthly_rate == 0.
    """
    factor = 1.0
    for h in range(1, months_beyond + 1):
        damped_rate = monthly_rate * (PROJ_DAMPING ** (h - 1))
        factor *= 1.0 + damped_rate
    return factor


def _project_forward(points: list[dict], target_date: date) -> float:
    """Extrapolate a CPI index value forward past the last observed bimonthly point.

    Uses a *recency-weighted* compound monthly rate derived from the last few
    observed Honolulu bimonthly points, then applies Holt damped-trend
    compounding (Gardner & McKenzie 1985) so the projected slope decays as the
    horizon lengthens. With only two points this collapses to the original
    single-pair compound rate — preserving the existing test contract.

    Rationale: Honolulu CPI is bimonthly and noisy. A pure 2-point trend
    chases the latest print; the smoothed-rate variant honours momentum
    while diluting one-off spikes. Damping caps the open-ended risk of
    compounding any positive trend forever — the empirical literature
    (Hyndman & Athanasopoulos 2018; Cleveland Fed WP 2406 on inflation
    nowcasting) consistently finds damped trends out-of-sample-beat
    undamped ones for short horizons on noisy macro series.

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

    monthly_rate = _smoothed_monthly_rate(ordered)
    if monthly_rate is None:
        return float(latest["value"])

    # Cap the per-period rate at ±0.0189/month (~±25% annualized) to stop a
    # single noisy bimonthly print from compounding into an unrealistic
    # extrapolation, then apply damped compounding over the horizon.
    monthly_rate = max(min(monthly_rate, PROJ_MONTHLY_CAP), -PROJ_MONTHLY_CAP)
    return float(latest["value"] * _damped_compound_factor(monthly_rate, months_beyond))


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
