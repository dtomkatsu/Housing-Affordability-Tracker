# Methodology

This document records the data-transformation choices that aren't self-evident
from the code — CPI series picks, forward-projection rules, rent-anchor
vintage, and the annual re-anchoring cadence. When a BLS release lags or a
fresh ACS vintage drops, this is the file to read before touching numbers.

## Data sources (authoritative)

| Domain | Source | Series / table | Cadence | Script |
|---|---|---|---|---|
| SFH & condo medians | Redfin Data Center (public S3) | `state_market_tracker.tsv000.gz`, `county_market_tracker.tsv000.gz` | monthly, 3rd-Friday release | `redfin-price-updater.py` |
| Contract rent (existing leases) | Census ACS 5-yr | `B25058_001E` | annual, December release | `redfin-price-updater.py` |
| Rent CPI (existing tenants) | BLS Honolulu MSA | `CUURS49ASEHA` | bimonthly (even months), NSA | `redfin-price-updater.py` |
| Asking rent | Zillow ZORI | `County_zori_uc_sfrcondomfr_sm_month.csv` | monthly | `redfin-price-updater.py` |
| HUD income limits | HHFDC county PDFs + HUD state PDF | FY 2025 MFI | annual | `redfin-price-updater.py` |
| Construction authorizations | DBEDT QSER | Table E-8 | annual (from quarterly data) | `redfin-price-updater.py` |
| All-items CPI (headline chip) | BLS Honolulu | `CUURS49ASA0` | bimonthly | `bls-cpi-updater.py` |
| Shelter / food / gasoline / transport CPI | BLS Honolulu | `CUURS49ASAH`, `CUURS49ASAF11`, `CUURS49ASETB01`, `CUURS49ASAT` | bimonthly | `bls-cpi-updater.py` |
| Thrifty Food Plan | USDA CNPP | Alaska-Hawaii monthly report | monthly | `tfp-updater.py` |
| Gas prices | AAA Hawaii | statewide average | daily | `gas-price-updater.py` |
| Grocery basket | In-house scrape, CPI-adjusted | `pipelines/grocery/` | ad-hoc + monthly CPI roll | `grocery-price-updater.py` |

The `CUURS49A*` prefix is **Honolulu Urban Hawaii, not seasonally adjusted**.
There is no neighbor-island CPI — every CPI adjustment applied to Maui,
Hawaii County, or Kauai uses the Honolulu ratio as a directional proxy.

---

## Rent-anchor year

**Single source of truth**: `RENT_ANCHOR_YEAR` constant at the top of
`redfin-price-updater.py`. Everything downstream — the Census ACS endpoint URL
and the BLS base-year average — derives from it.

We fetch the Honolulu (and every other county's) ACS anchor dollar value
**live** from the Census API each run, so the dollar value cannot drift out
of sync with the anchor year. No hardcoded dollar figures live in the repo.

### Re-anchoring cadence

Update `RENT_ANCHOR_YEAR` **once per year, in December or January**, when
the new ACS 5-year vintage is published. Steps:

1. Bump the constant:
   ```python
   RENT_ANCHOR_YEAR = "2025"   # or whatever the new vintage is
   ```
2. Do a dry-run: `python3 redfin-price-updater.py --dry-run` and confirm the
   printed "anchor ACS {year} $X,XXX × ratio {r}" line reflects the new vintage.
3. Sanity-check the four counties: moving from vintage N to N+1 should shift
   each county's anchor by a small single-digit %. A 20%+ jump means Census
   hasn't published the new vintage yet, or you're hitting a cached URL.
4. Run end-to-end and verify the dashboard's rent figures move sanely.

**Why re-anchor at all?** The BLS Honolulu rent CPI is an *index*, not a
dollar value — it only tells us "rent today is X% of rent in 1982-84." To
convert that into dollars we multiply by an ACS dollar anchor. The ratio
`BLS(now) / BLS(anchor_year_avg)` compounds any indexing error in proportion
to how far "now" is from "anchor_year". Re-anchoring annually keeps the
extrapolation window short and the absolute-dollar reading tight to the
most recent hard Census observation.

### Why the anchor year is duplicated in two constants

`CENSUS_ACS_YEAR` and `BLS_BASE_YEAR` both equal `RENT_ANCHOR_YEAR` today —
that's intentional and required: the index-to-dollar conversion is only
valid when the ACS dollar year and the BLS base-average year are the same.
The two named constants exist so the intent reads clearly at each use site.
**Never set them to different years** without fully re-deriving the scaling.

---

## Forward-projection rule (groceries)

### Why we project

The grocery pipeline CPI-adjusts baseline prices each month via Honolulu
bimonthly BLS series (food-at-home, dairy, meat-poultry-fish-eggs, etc.).
When the dashboard's target month falls **past the latest observed
bimonthly period** — e.g. target April, latest release February — there are
only two honest choices: refuse to update the card, or extrapolate with an
explicit flag.

The previous implementation silently took "no change since last
observation," which hid a flat-line assumption from the reader. We now
extrapolate **linearly** (in log space) and surface a `proj.` tag on the
card so the user knows.

### How it works

`pipelines/grocery/src/price_adjuster.py :: compute_cpi_ratio()` returns a
dict with `method ∈ {exact, interpolated, projected, unavailable}`. The
projection path uses the last two observed bimonthly points to derive a
per-month compound growth rate, then rolls the latest index value forward:

```
monthly_rate  = (latest / prev) ** (1 / months_between) - 1
monthly_rate  = clamp(monthly_rate, ±0.0189)          # ≈ ±25%/yr cap
projected_idx = latest * (1 + monthly_rate) ** months_beyond
```

The `±0.0189/month` cap stops a single noisy bimonthly print from
compounding into an unrealistic three-month extrapolation.

### How it surfaces in the UI

`scripts/update_prices.py` writes `data/output/cpi_status.json` — a small
sidecar containing `is_projected`, `latest_actual_period`, and per-category
method. `grocery-price-updater.py` reads that sidecar and writes
`projected: true`/`originalPeriod: "YYYY-MM"` into each county's
`groceryData` block. The HTML's as-of popover formatter
(`fmtPeriodText`) then renders an amber "proj." pill on the grocery row,
using the same period-tag style already used by the USDA TFP card.

If the sidecar is missing (older pipeline run), the updater treats the
state as "not projected" — graceful degradation.

---

## Rent nowcast blend (70% BLS CPI + 30% ZORI)

The BLS Honolulu rent CPI lags market asking rent by roughly 12 months — it
samples each unit once every six months and averages continuing leases
alongside new ones. Zillow ZORI is an asking-rent index that leads CPI but
overreacts to turnover.

We blend the two, anchored to the same ACS 2024 dollar base:

```
blended_rent = ACS_anchor × ( 0.7 × BLS_ratio  +  0.3 × ZORI_ratio )
```

where each ratio is `latest / anchor_year_average`. The 70/30 split
captures most of the CPI lag without letting asking-rent swings dominate
the reported number. See Cleveland Fed WP 22-38r ("New-Tenant Repeat Rent
Inflation") for the academic basis.

Fallback chain:
- BLS fetch fails → ACS raw values stay (no monthly currency)
- ZORI fetch fails → CPI-only scaling (lagging but consistent)
- County missing 2024 ZORI baseline (Kauai often) → use state ZORI ratio
  as proxy, analogous to how Honolulu BLS rent CPI is already applied
  statewide

---

## Grocery basket: effective price

Published prices are the all-in consumer cost:
1. Start with **member/loyalty prices** (the prices actually paid at the
   register — Foodland Maika'i, Safeway Club, Costco membership).
2. Aggregate per county via **market-share weights**
   (`config/store_weights.json`, built from SNAP retailer list + Census CBP
   employment cross-check).
3. Apply Hawaii **General Excise Tax (4.5%)** at checkout — GET hits
   groceries at the register, unlike most US states that exempt food.

The dashboard's `basketWithTax` field is the post-GET, post-weighting number;
`basketPretax` is the pre-GET subtotal for audit.

---

## Quarterly NTR/ATR benchmark refresh

### What this is

`data/ntr_atr_benchmarks.json` is a hand-maintained sanity-check file holding
the latest **national** YoY from BLS's two research rent series:

- **R-CPI-NTR** — *New Tenant Repeat Rent*. Reprices only units that
  transitioned to a new tenant in the quarter. Closest national analog to
  our ZORI-heavy asking-rent signal.
- **R-CPI-ATR** — *All Tenant Regressed Rent*. Hedonic-regression-adjusted
  all-tenant rent. Leads the official CPI rent series by roughly one quarter.

Both are **national only** — BLS does not publish a Hawaii cut. We use them
as a directional guardrail on our Honolulu nowcast, not as ground truth.

### Why manual refresh

Both series are published as XLSX files on
[bls.gov/cpi/research-series](https://www.bls.gov/cpi/research-series/r-cpi-ntr.htm).
BLS's public API does **not** expose the research series, and the XLSX
endpoints are gated by Akamai anti-bot rules that reject `curl`/`urllib`/
WebFetch regardless of User-Agent. A quarterly human refresh is simpler
and more reliable than fighting the anti-bot layer in CI.

### Refresh cadence

- **Timing**: quarterly, on or after the 15th of the month following each
  quarter-end (Jan / Apr / Jul / Oct). Data has a 1-quarter lag.
- **Who**: anyone bumping data for the dashboard around that time; the
  `_refresh_howto` array in `ntr_atr_benchmarks.json` is the checklist.

### Steps

1. Visit <https://www.bls.gov/cpi/research-series/r-cpi-ntr.htm> and download
   the latest R-CPI-NTR and R-CPI-ATR XLSX files.
2. Open each XLSX. The rightmost column is the just-published quarterly
   release (e.g. `2025Q4`).
3. YoY % = (latest_quarter / same_quarter_prior_year − 1) × 100.
4. Edit `data/ntr_atr_benchmarks.json`:
   - `latest_quarter`  → release quarter string (e.g. `"2025Q4"`)
   - `ntr_yoy_pct`     → NTR YoY %
   - `atr_yoy_pct`     → ATR YoY %
   - `last_refreshed`  → today's ISO date
   - `refreshed_by`    → your name or email
5. Commit the change. The next `redfin-price-updater.py` run will consume
   the new values and print them in the "Rent sanity check" block.

### How the audit uses the benchmark

`audit_rent_nowcast_vs_ntr()` in `redfin-price-updater.py` runs after the
blended-nowcast block on every run. It prints a 5-row table: Honolulu rent
CPI YoY, Honolulu ZORI YoY, national NTR YoY, national ATR YoY, and the
first-order approximation of the Honolulu blended YoY
(`w·CPI_YoY + (1−w)·ZORI_YoY` with w=0.7).

If `|blended − ntr| > sanity_band_pp` (default ±8 pp), the updater prints
a `⚠ WARNING` line. The warning does **not** block the run — Hawaii rent
inflation routinely runs hotter or cooler than the national average — but
a gap far outside the band is the clearest signal that either our weights
need retuning or an upstream data source broke.

When the benchmark JSON still has null NTR/ATR values (e.g. first commit,
or a quarter you haven't refreshed yet), the audit prints the Honolulu
numbers anyway and hints at the refresh path so the blind spot is visible.

---

## Change control

Any time you bump a vintage, change a series ID, or alter a weight —
update this file **in the same commit** as the code change. Readers
(including future-you) look here first to understand why a reported number
moved.
