# Scraper Notes

Research findings on how each chain's website works, to inform scraper architecture.

## Store-by-Store Findings

### Foodland (`shop.foodland.com`) ✅ Easiest
- **UPC in URL**: Product pages follow the pattern `/product/{name}-id-{UPC}`
  - Example: `/product/meadow-gold-dairyland-lactose-free-whole-milk-id-810133300232`
  - The 12-digit number at the end of the URL is the UPC
- **Search**: `https://shop.foodland.com/results?q={query}`
- **Price fields**: name, price, sale price, member price all render without bot blocking
- **Strategy**: Search by UPC query or product name, extract price from result page
- Operates in all 4 counties — primary scraping anchor

### Safeway (`safeway.com`) ⚠️ Moderate difficulty
- Bot protection (Incapsula) blocks simple HTTP fetches
- Product IDs in URLs are internal Safeway IDs (e.g., `136010121`), not UPCs
- **Strategy**: Use browser automation (Playwright/Puppeteer) or look for Albertsons/Safeway unofficial API
- Safeway and Albertsons share the same platform — any Albertsons API workaround applies
- Covers Honolulu, Maui, Kailua-Kona, Hilo, Lihue

### Walmart (`walmart.com`) ⚠️ Moderate difficulty
- CAPTCHA wall blocks automated fetches
- Walmart item numbers in URLs (e.g., `/ip/.../10450114`) — not UPCs
- **Strategy**: Walmart's product API endpoint: `https://www.walmart.com/ip/{item-id}` with proper headers, or use Walmart affiliate/partner API
- Covers Honolulu (Keeaumoku), Kahului, Kailua-Kona, Hilo, Lihue

### Times/Big Save (`timessupermarkets.com`) — TBD
- "Times" on Oahu/Maui; "Big Save" on Kauai (same parent company)
- Need to investigate whether they have an online shop with scrapable prices
- Covers Honolulu (Beretania), Maui, Kapaa (Kauai)

## Milk: Cross-Chain UPC Strategy

Since milk is sold under store brands at each chain, track 3 products per slot:

| Chain | Whole Milk Product | UPC |
|-------|--------------------|-----|
| Foodland / Times | Meadow Gold (name match) or DairyPure | 041900076382 |
| Safeway | Lucerne Whole Milk 1 Gallon | 021130070022 |
| Walmart | Great Value Whole Milk 1 Gallon | 078742351865 |

| Chain | 2% Milk Product | UPC |
|-------|--------------------|-----|
| Foodland / Times | Meadow Gold (name match) or DairyPure | 041900076610 |
| Safeway | Lucerne 2% 1 Gallon | 021130070039 (estimated — verify) |
| Walmart | Great Value 2% 1 Gallon | 078742351872 |

## Store Targets by County

| County | Safeway | Walmart | Foodland | Times/Big Save |
|--------|---------|---------|----------|----------------|
| Honolulu | S. Beretania | Keeaumoku | Ala Moana Farms | Beretania |
| Maui | Wailuku | Kahului | Kahului | — |
| Hawaiʻi | Kailua-Kona + Hilo | Kailua-Kona + Hilo | Mauna Lani | — |
| Kauaʻi | Lihue | Lihue | Princeville | Kapaʻa Big Save |

## Suggested Scraper Architecture

1. **Foodland first** — easiest, serves all 4 counties, UPC in URL
2. **Safeway** — browser automation, one store per county
3. **Walmart** — browser automation or API, one store per county
4. **Times/Big Save** — investigate online presence

For items with `scrape_strategy: "name_match"`, fall back to text search on each store's website and pick the closest product by name + size.
