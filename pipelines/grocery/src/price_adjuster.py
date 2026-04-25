"""Apply CPI-based adjustments to baseline prices.

The projection / smoothing / damping / cap-firing logic — and the
calibrated `_PROJ_SE_INFLATOR` — all live in the standalone
`census_forecaster` package now. This module is a thin local consumer:

* `load_baseline()` — loader for the grocery basket CSV.
* `compute_cpi_ratio` — re-exported from `census_forecaster.bls.projection`
  so existing callers in this repo keep working without churn.
* `adjust_prices()` — applies the per-category CPI ratio to baseline
  prices and returns the per-category projection-state metadata.

Underscore-prefixed names (`_smoothed_monthly_rate`, `_project_forward_full`,
…) are kept as aliases over the package's public names so the existing
test suite and internal callers don't break.
"""

import csv
from datetime import date
from pathlib import Path

# All projection logic now lives in the Census Forecaster package.
# Cross-cutting calibration changes (rate cap, damping, SE inflator)
# happen upstream and propagate here automatically.
from census_forecaster.bls.projection import (
    PROJ_DAMPING,
    PROJ_MONTHLY_CAP,
    ProjectionResult,
    _PROJ_SE_INFLATOR,
    _RESIDUAL_LOG_STD_PRIOR,
    _Z_90,
    compute_cpi_ratio,
    damped_compound_factor as _damped_compound_factor,
    forecast_se_log as _forecast_se_log,
    project_forward as _project_forward,
    project_forward_full as _project_forward_full,
    residual_log_std as _residual_log_std,
    smoothed_monthly_rate as _smoothed_monthly_rate,
)

from .cpi_fetcher import find_nearest_periods, get_cpi_value, date_to_bls_period
from .models import AdjustedPrice, BaselinePrice, BasketConfig, CPIConfig


__all__ = [
    "PROJ_DAMPING",
    "PROJ_MONTHLY_CAP",
    "ProjectionResult",
    "compute_cpi_ratio",
    "load_baseline",
    "adjust_prices",
    "_damped_compound_factor",
    "_forecast_se_log",
    "_project_forward",
    "_project_forward_full",
    "_residual_log_std",
    "_smoothed_monthly_rate",
    "_PROJ_SE_INFLATOR",
    "_RESIDUAL_LOG_STD_PRIOR",
    "_Z_90",
]


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


def adjust_prices(
    baseline_prices: list[BaselinePrice],
    cpi_data: dict,
    cpi_config: CPIConfig,
    basket: BasketConfig,
    target_date: date,
) -> tuple[list[AdjustedPrice], dict]:
    """Adjust all baseline prices to a target date using CPI ratios."""
    adjusted = []

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
                    "cap_fired": None, "monthly_rate": None,
                    "implied_annual_rate": None, "forecast_se": None,
                    "ratio_ci90_low": None, "ratio_ci90_high": None,
                    "horizon_months": None,
                }
                continue
            series_id = cat_config["series_id"]
            base_date = date.fromisoformat(bp.date)
            ratios[cpi_cat] = compute_cpi_ratio(cpi_data, series_id, base_date, target_date)

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
