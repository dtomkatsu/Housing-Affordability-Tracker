# Hawaii Grocery Price Tracker

County-level grocery price index for all 4 Hawaii counties (Honolulu, Maui, Hawaii/Big Island, Kauai). No county-level food price data currently exists for Hawaii — BLS CPI only covers the Honolulu metro area.

## Methodology

A **fixed basket** of 85 grocery items tracks prices across stores in every county. The basket is organized by USDA Thrifty Food Plan commodity groups, adjusted for Hawaii's food culture:

| Category | Items | Weight | Notable Items |
|----------|-------|--------|---------------|
| Grains | 11 | 16% | Calrose rice, Jasmine rice, Cheerios |
| Vegetables | 11 | 12% | Fresh produce (PLU-coded) + canned/frozen |
| Fruits | 9 | 8% | Bananas, Dole pineapple, Tropicana OJ |
| Dairy | 9 | 10% | Meadow Gold milk (local), eggs, butter |
| Protein–Meat | 10 | 19% | SPAM Classic, chicken thighs, pork shoulder |
| Protein–Seafood | 4 | 6% | Canned tuna, salmon, frozen shrimp |
| Protein–Other | 4 | 5% | Firm tofu, pinto beans, peanut butter |
| Fats & Oils | 3 | 3% | Vegetable/canola oil, margarine |
| Beverages | 6 | 8% | Bottled water, Folgers coffee, ITO EN tea |
| Condiments | 6 | 5% | Kikkoman soy sauce, kimchi, Best Foods mayo |
| Sugars & Sweets | 3 | 2% | C&H sugar, Smucker's jam |
| Snacks | 3 | 3% | Lay's chips, Diamond Bakery arare |
| Prepared | 6 | 3% | Campbell's soup, Maruchan ramen, SPAM |
| **Total** | **85** | **100%** | |

### Hawaii-Specific Design

- **Rice**: Two varieties (Calrose + Jasmine) — Hawaii consumes 2-3x more rice per capita than mainland
- **SPAM**: Hawaii consumes ~7M cans/year; present at every chain statewide
- **Meadow Gold dairy**: Produced on Oahu — the Honolulu-to-outer-island price spread directly measures inter-island freight cost impact
- **Asian pantry staples**: Soy sauce, tofu, kimchi, arare, ITO EN green tea, ramen
- **Plate lunch culture**: Chicken thighs (bone-in), pork shoulder, cabbage

### Chain Coverage

| Chain | Honolulu | Maui | Big Island | Kauai |
|-------|----------|------|------------|-------|
| Foodland | x | x | x | x |
| Costco | x | x | x | x |
| Times/Big Save | x | x | | x |
| Safeway | x | x | x | |
| KTA Super Stores | | | x | |
| Walmart | x | x | x | |
| Don Quijote | x | | | |

### Inter-Island Freight Sensitivity

Each item is classified into freight sensitivity tiers:
- **Tier A** (20-30%+ outer-island premium): Fresh meat, dairy, produce, refrigerated items
- **Tier B** (10-20%): Heavy shelf-stable — 5lb rice, water, soda, oil
- **Tier C** (5-15%): Light shelf-stable — canned goods, cereal, condiments

## Project Structure

```
hawaii-price-tracker/
├── basket_definition.json    # 85-item basket with brands, sizes, UPCs, substitutions
├── upc_registry.json         # Confirmed barcodes from Open Food Facts / GS1
├── index_weights.json        # Category weights (Hawaii-adjusted from USDA TFP)
├── freight_sensitivity.json  # Freight tier classification per item
├── .gitignore
└── README.md
```

## UPC Resolution

Each basket item is mapped to a concrete barcode:
- **Packaged goods**: 12-digit UPC-A from Open Food Facts, UPCitemdb, or GS1
- **Fresh produce**: Standardized PLU codes (e.g., PLU 4011 for bananas)
- **Fresh meat**: Priced by posted $/lb at each store

Resolution pipeline: Open Food Facts API → UPCitemdb → GS1 Verified → manual shelf validation

## Data Collection

Prices are collected per UPC/PLU at each store, recording:
- Total price
- Unit size (from product listing)
- Computed per-unit price (normalized)
- Date, store, county
- Substitution flag (if alternate product used)

Missing observations (UPC not found at a store) are recorded as missing, never as zero.

## Index Computation

1. Normalize all prices to per-unit (per oz, per lb, per fl oz, per count)
2. Average per-unit prices within each category by county
3. Apply Hawaii-adjusted category weights
4. Produce composite index per county per collection period

## Rebase Schedule

- **Quarterly**: Verify each UPC is still active at majority of stores
- **When retiring a UPC**: Record 2-week overlap with replacement, splice using overlap ratio
- **Annual**: Review category weights against latest USDA TFP and Hawaii CES data
