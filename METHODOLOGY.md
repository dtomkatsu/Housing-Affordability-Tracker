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
| Typical-household FAH spending (side-stat) | BLS CE PUMD interview survey | Honolulu PSU `S49A`–`S49D`, FINLWT21-weighted, 5y pool | annual, target October | `pipelines/grocery/scripts/refresh_ce_pumd.py` |

The `CUURS49A*` prefix is **Honolulu Urban Hawaii, not seasonally adjusted**.
There is no neighbor-island CPI — every CPI adjustment applied to Maui,
Hawaii County, or Kauai uses the Honolulu ratio as a directional proxy.

The grocery pipeline's `cpi_series.json` still uses the **legacy area-426
codes** (`CUUSA426SAF11`, etc.). BLS continues to publish both the legacy
A426 series and the post-2018 S49A series in parallel; treat them as
equivalent for nowcast purposes. If BLS ever sunsets one of the prefixes,
mirror the other before re-running the pipeline.

---

## CPI release cadence (Honolulu, area S49A)

Every Honolulu CPI series consumed here is **bimonthly**, not monthly.

* **Data periods**: odd months only — Jan, Mar, May, Jul, Sep, Nov.
  There are no Feb / Apr / Jun / Aug / Oct / Dec observations.
* **Release**: each odd-month data point is published on or near the **15th
  of the following even month**. So Mar-2026 data lands ~Apr-15, 2026.
* **YoY**: same odd-month one year prior is always available, so
  `compute_yoy` in `bls-cpi-updater.py` doesn't need interpolation.

Two practical consequences for the pipeline:

1. `pipelines/grocery/src/cpi_fetcher.py :: expected_latest_period()` keys
   off the odd-month set (`BLS_DATA_MONTHS = {1,3,5,7,9,11}`). A previous
   bug had this set to even months, which made the cache check ask BLS for
   data points it never publishes — every run silently re-fetched.
2. When the dashboard's reference month is even (e.g. April), every
   downstream metric that depends on Honolulu CPI (groceries, TFP, BLS rent
   nowcast) is **always at least one month past the latest observation**.
   This is the case the projection / interpolation logic in
   `price_adjuster.py` and `tfp-updater.py` exists to handle — see the
   forward-projection rule below for the math and the per-month cap.

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
projection path computes a **recency-weighted smoothed monthly rate**
across the last few bimonthly observations, then applies **Gardner-McKenzie
damped-trend** compounding so the slope decays as the forecast horizon
grows. With exactly two points the smoothed rate collapses to the original
single-pair rate — a deliberate back-compat path — and with three or more
points the prior trend dilutes a single noisy bimonthly spike:

```
# (1) Pairwise compound rates from each adjacent pair
rates_i  = (p_i.value / p_{i-1}.value) ** (1 / months_i) - 1

# (2) Most recent pair gets weight 1.0; each step back halves it
weight_i = 0.5 ** ((n-1) - i)

# (3) Recency-weighted geometric mean
monthly_rate = Σ(rate_i * weight_i) / Σ(weight_i)

# (4) Cap and damp the projection slope each month forward
monthly_rate  = clamp(monthly_rate, ±0.0189)              # ≈ ±25%/yr cap
projected_idx = latest * Π_{h=1..H} (1 + monthly_rate * φ^(h-1))
                where φ = 0.92  (damping factor)
```

The **±0.0189/month cap** stops a single noisy bimonthly print from
compounding into an unrealistic three-month extrapolation. Concretely:

```
(1 + 0.0189) ** 12 ≈ 1.252   →   +25.2 %/yr ceiling
(1 − 0.0189) ** 12 ≈ 0.795   →   −20.5 %/yr floor
```

The **damping factor φ = 0.92** is from Gardner & McKenzie (1985) and is
the standard default in Holt damped-trend (`ets(damped=TRUE)` in R, the
`Holt(damped_trend=True)` initializer in `statsmodels`). It bounds the
open-ended risk of any positive trend compounding forever — by month 6
only ~61% of the latest momentum is applied; by month 12, ~37%. Hyndman &
Athanasopoulos (2018) and Cleveland Fed WP 24-06 both report damped trends
out-of-sample-beat undamped ones for short-horizon noisy macro series. For
the typical 1–2-month projection horizon in this repo the damping effect
is small (~1–2% on a 1%-per-month trend) but it eliminates a tail risk on
the rare runs where reference-month is 4+ months past the latest BLS print.

The same smoothing + cap + damping is applied in
`tfp-updater.py :: _cpi_value_for()` when the reference month is past the
latest BLS Honolulu food-CPI observation — keeps every CPI-driven projection
in this repo on the same momentum ceiling.

**Cross-module note.** The `census_forecasting/` package uses the same
Gardner-McKenzie damping discipline on its own cadence — φ=0.85 *per
year* there, vs φ=0.92 *per month* here. Trend half-lives (~8.3 months
for CPI, ~4.3 years for ACS) reflect each source's signal-to-noise. The
recency-weighted pairwise-rate smoother described above is also used to
initialize the damped-trend fit in census forecasting; see
`census_forecasting/METHODOLOGY.md` §2.3 and §2.3.1.

### What we do *not* use, and why

**Machine learning (LSTM, XGBoost, Random Forest)** for projecting Honolulu
CPI bimonthlies past the latest print: too little training data. The
Honolulu S49A series only goes back to ~2018 (CPI area-code restructuring
released the modern S49A codes that year), giving ~50 bimonthly
observations per series. Recent literature (e.g. *Modeling inflation with
machine learning: a cross-horizon systematic review*, IJDSA 2025) finds
LSTMs underperform AR/SARIMA and ridge on small-sample inflation data,
overfitting noise without a meaningful gain at short horizons. Tree
ensembles (RF, XGBoost) fare better but require multivariate features
(national CPI, oil futures, gas prices) that we already incorporate
upstream of the projection — adding the same signals back through a model
would double-count them. The `±0.0189/month` cap, recency-weighted slope,
and 0.92 damping together approximate a damped-Holt point forecast, which
the academic consensus says is the right baseline class for this kind of
short-horizon, small-sample series.

**Seasonal adjustment (X-13ARIMA-SEATS)** before projecting: the BLS
Honolulu S49A series we consume are NSA (not seasonally adjusted), but the
projection horizon is at most ~3 months and the YoY chip on the dashboard
already implicitly absorbs seasonality (same calendar month, year-over-year).
A seasonal decomposition would need ≥3 years of clean data; with only ~50
points the seasonal factor estimate is noisier than the noise it removes.

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

### Walk-forward backtest

`backtests/rent_blend_walkforward.py` runs a Cleveland-Fed-style
pseudo-out-of-sample evaluation of the 70/30 weight. For each anchor
T ∈ {2022-04, 2022-10, 2023-04, 2023-10, 2024-04} the harness:

1. Pulls BLS rent CPI, ZORI, and the ACS 5-year vintage that was actually
   live at T (vintage_year+1 December release rule).
2. Runs `blend_rent_nowcast()` to project T+12 rent.
3. Compares the projection against realized rent at T+12 under two
   ground-truth views:
   - **Blend-truth** = `(BLS_T+12 + ZORI_T+12) / 2` (symmetric)
   - **BLS-only-truth** = `BLS_T+12` (anchored to the slow-moving series
     that already lags ~12 months, so BLS_T+12 ≈ true rent at T)

5 anchors × 5 regions = 25 cells per scheme. Five schemes evaluated:
BLS-only, 70/30, 60/40, 50/50, ZORI-only. Results in
`backtests/results/rent_blend_2026-04.md`.

The current results (April 2026 run): under blend-truth ZORI-only "wins"
on MAPE but the metric is mechanically biased toward whichever input
dominates the ground truth; under BLS-only-truth the live 70/30 weight
sits at ~3.3% MAPE versus 2.9% for 50/50 — a small enough gap that the
existing 70/30 weight remains defensible. No auto-tuning of the live
constant; weight changes go through a separate review.

Refresh cadence: rerun annually after a new ACS vintage drops, then
again any time the blend logic changes. Cached BLS/ACS responses live in
`backtests/cache/` so reruns are deterministic.

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

## BLS CE PUMD "typical household" side-statistic

### What this is

`pipelines/grocery/data/pumd_honolulu_monthly.json` holds a **separate**
benchmark of average monthly food-at-home (FAH) spending per Honolulu
household, derived from the BLS Consumer Expenditure Public Use Microdata
(CE PUMD) interview-survey microdata. The dashboard's grocery card surfaces
this as a "Typical: $X/mo per BLS CE PUMD" line under the existing
`monthlyFamily4` derived from our receipt basket.

**This is a side-statistic only.** It does **not** drive any per-item
pricing, does **not** modify the receipt basket, and does **not** change
the headline `basketWithTax` or `monthlyFamily4` numbers. It exists so
readers can compare the basket-derived family-of-4 cost against an
independently measured household spending figure.

### Why it's separate from the basket

- **Receipts** measure *prices* — what a specific item costs at a specific
  store on a specific date. Per-item, per-category, per-county granularity.
- **PUMD** measures *spending* — what households actually pay for groceries
  per month, including substitution, brand choice, and basket composition
  effects we can't capture in a fixed basket. PSU resolution: only Urban
  Honolulu is identifiable in PUMD.

The two answer different questions, and we surface both rather than
calibrating one against the other.

### How the figures are derived

`pipelines/grocery/scripts/refresh_ce_pumd.py` orchestrates a full
microdata refresh:

1. **Download** the 5 most recent annual interview-survey ZIPs from
   `https://www.bls.gov/cex/pumd/data/comma/intrvw{yy}.zip` (default
   2019–2023; ~30 MB each, written to `data/pumd_raw/` which is
   git-ignored).
2. **Filter** FMLI rows to PSU codes for Urban Honolulu (`S49A`–`S49D`).
3. **Aggregate** food-at-home directly from MTBI (per BLS errata for
   2023+, the `FDHOMEPQ`/`FDHOMECQ` summary columns were stripped):
   sum UCCs whose hierarchical-grouping code starts with `19` and excludes
   `1909*` (groceries on trips).
4. **Per-household monthly FAH** = sum(MTBI FAH UCCs) / 3 (each FMLI row
   is a quarterly interview).
5. **Inflation-adjust** each year to the latest period via the Honolulu
   food CPI series `CUURS49ASAF11`.
6. **Apply CE-recommended `FINLWT21` weights** for population-representative
   means; pool 5 years to mitigate small Honolulu PSU sample size
   (~50–200 households/quarter; 5y pool gives ~1.5–4k Honolulu HH-quarters).
7. **Stratify** by family size: 1, 2, 3, 4+ buckets.

### Neighbor-island projection

PUMD only resolves to Honolulu; Maui, Hawaii, and Kauai household samples
don't exist. We project the Honolulu PUMD value to the neighbor islands
using the **receipt-derived basket gradient**:

```
county_factor[c]   = basket_total[c] / basket_total[Honolulu]
pumd_estimate[c]   = pumd_honolulu × county_factor[c]
state_estimate     = population-weighted mean over the four counties
```

This preserves PUMD as the **absolute-level anchor** (real measured
Honolulu spending) and the receipts as the **spatial gradient** (real
measured price gaps across counties). Both inputs are real data; the
combination is internally consistent.

### Refresh cadence

- **Timing**: annually, target October. BLS releases each PUMD year ~9–12
  months after collection ends; new full-year data typically lands in
  September–October.
- **Window**: each refresh shifts the 5-year pool forward by one year.
  E.g. the 2026 refresh uses 2020–2024 once 2024 is published.
- **Command**:
  ```bash
  python3 pipelines/grocery/scripts/refresh_ce_pumd.py --years 2020 2021 2022 2023 2024
  ```
  This downloads ~150 MB of raw data, runs the extractor, and overwrites
  `pumd_honolulu_monthly.json`.

### Bootstrap-vs-microdata distinction

The current `pumd_honolulu_monthly.json` carries
`method: "bootstrap_from_published_aggregates_pending_microdata_refresh"`.
That means the figures were derived from BLS CES 2022-23 published
Honolulu MSA aggregates (Table 3204 metro-area patterns), inflated to
2024-12 via the Honolulu food CPI, and projected across counties via the
basket gradient.

The bootstrap exists because the BLS PUMD ZIP endpoint is blocked from
this development environment by Akamai access controls. Running the
refresh script from an unblocked network (residential, etc.) will:

- Replace `method` with `5y_pooled_finlwt21_inflated_to_as_of`
- Populate `n_households_total` with the pooled sample count
- Populate `honolulu_ci_95_overall` and `honolulu_ci_95_family4` with
  bootstrap 95% confidence intervals from the FINLWT21 weighted means

The pipeline tolerates either form: the JSON's `byCounty` numbers feed the
dashboard tile regardless of method, and the methodology popover annotates
the source.

### Sample-size caveats

- Honolulu PSU draws ~50–200 households per quarterly interview wave.
- 5-year pooling gives ~1,500–4,000 HH-quarters of FAH observations — wide
  enough for a single Honolulu mean but **not** enough to support fine
  cross-tabulation (e.g. family size × dwelling type × income tertile).
- Family-size buckets (1 / 2 / 3 / 4+) are usable; deeper splits would
  shrink CIs past the point of usefulness.
- Neighbor-island projections inherit the basket gradient's uncertainty —
  treat them as directional rather than precise.

### Failure modes

- Missing JSON → grocery pipeline logs and continues; "Typical" line is
  omitted from the card. No exception, no broken render.
- Corrupt JSON → same graceful fallback.
- Refresh script can't reach BLS → bootstrap stays in place; the file's
  `note` field documents the situation.

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

## Unit conventions (cadence + conversions)

To keep the dashboard internally consistent, every number flows through
one of three native cadences:

| Domain | Native cadence | Where to convert |
|---|---|---|
| Sale prices (Redfin) | monthly | n/a — already monthly |
| Asking rent (ZORI) | monthly | n/a |
| Existing-tenant rent (BLS) | bimonthly (odd months) | always reported as nowcast for the latest odd month |
| Headline / shelter / food / energy / transport CPI | bimonthly (odd months) | YoY only; no resampling |
| TFP (USDA CNPP) | monthly | rolled forward via Honolulu food CPI when stale |
| Grocery basket | monthly target, weekly published | `WEEKS_PER_MONTH = 52/12` |
| HUD income limits | annual (FY) | annual % of income → /12 for monthly comparisons |
| DBEDT construction auth | annual | n/a |
| AAA gas | daily snapshot | n/a |

**`WEEKS_PER_MONTH` constant**: `52 / 12 = 4.3333…`. Used in two places
that must stay reciprocal:

* `grocery-price-updater.py` converts the priced basket from weekly to
  monthly: `monthly_family4 = weekly * WEEKS_PER_MONTH`.
* The HTML `renderGoodsPane()` does the inverse for the TFP-anchored
  weekly display: `tfpWeekly = tfpMonthly / WEEKS_PER_MONTH`.

If you change the constant in one place, change it in the other; otherwise
the headline weekly figure will silently drift away from `monthlyFamily4 /
4.33`.

**Annualized vs monthly growth**: every per-month rate in the projection
code (`price_adjuster.py`, `tfp-updater.py`) is a *compound* rate, not a
simple one. `monthly_rate = (latest / prev) ** (1 / months_between) - 1`
and `projected = latest * (1 + monthly_rate) ** months_beyond`. Don't mix
this with the annualized YoY surfaced in the headline chip — those are
12-month log-equivalent aggregations that already include compounding.

**Rent nowcast cadence**: even though BLS rent CPI is bimonthly and ZORI
is monthly, both ratios are applied to the same `RENT_ANCHOR_YEAR` ACS
dollar anchor and combined in *level space*, so the blended figure is
re-published on every monthly run regardless of whether BLS issued a fresh
print that month — the BLS half just carries the latest odd-month ratio
forward until the next release lands.

---

## Change control

Any time you bump a vintage, change a series ID, or alter a weight —
update this file **in the same commit** as the code change. Readers
(including future-you) look here first to understand why a reported number
moved.
