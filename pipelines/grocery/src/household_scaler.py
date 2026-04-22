"""Scale basket costs by household composition using USDA TFP factors."""

from .models import AdjustedPrice, HouseholdConfig, HouseholdEstimate


def compute_household_costs(
    adjusted_prices: list[AdjustedPrice],
    household_config: HouseholdConfig,
) -> list[HouseholdEstimate]:
    """Compute grocery cost estimates for each household type and county.

    Groups adjusted prices by (county, geoid, date), sums basket total,
    then applies household scaling factors.
    """
    # Group by county + date to get basket totals
    county_baskets: dict[tuple[str, str, str], float] = {}
    for ap in adjusted_prices:
        key = (ap.county, ap.geoid, ap.adjusted_date)
        county_baskets[key] = county_baskets.get(key, 0.0) + ap.adjusted_price

    # Generate household estimates for each county
    estimates = []
    for (county, geoid, adj_date), basket_total in county_baskets.items():
        for hh_type, hh_data in household_config.household_types.items():
            factor = hh_data["effective_factor"]
            household_cost = round(basket_total * factor, 2)

            estimates.append(HouseholdEstimate(
                household_type=hh_type,
                household_label=hh_data["label"],
                county=county,
                geoid=geoid,
                date=adj_date,
                basket_total=round(basket_total, 2),
                household_cost=household_cost,
                effective_factor=factor,
            ))

    return estimates
