# census_forecasting

Project forward ACS census estimates with calibrated uncertainty.
Hawaii-focused defaults; the method generalises to any state/county
ACS panel.

## What this gives you

* **Point estimates and 90% prediction intervals** for ACS indicators
  at horizons of 1-5 years, defaulting to ACS 2024 → 2026.
* A **damped log-trend + AR(1) ensemble** that beats every naive
  baseline (carry-forward, OLS) on 96-fold walk-forward back-tests.
* **MOE propagation** through the forecast — sample uncertainty from
  the ACS-published margin of error fused with model forecast
  variance.
* **Reproducibility** — cached ACS responses are committed, all
  numbers pinned in tests, the empirical SE calibration multiplier
  is documented and re-derivable.

See [METHODOLOGY.md](METHODOLOGY.md) for the full design rationale,
references, and limitations.

## Quick start

```bash
# Run the test suite (offline; uses the committed cache)
python3 -m pytest census_forecasting/tests/ -v

# Generate Hawaii 2026 projections (4 counties × 4 indicators)
python3 census_forecasting/scripts/project_acs_2026.py

# Re-run the walk-forward back-test
python3 census_forecasting/scripts/run_backtest.py
```

## Layout

```
census_forecasting/
├── METHODOLOGY.md          design rationale, references, limitations
├── README.md               this file
├── src/
│   ├── moe.py              MOE → SE conversion + Census Handbook propagation
│   ├── models.py           AcsObservation / GeographySeries / ForecastPoint
│   ├── acs_client.py       ACS API client with on-disk cache
│   ├── projection.py       damped log-trend + AR(1) on log-diffs
│   ├── ensemble.py         inverse-variance combiner + macro anchors
│   └── backtest.py         walk-forward harness with naive baselines
├── scripts/
│   ├── project_acs_2026.py production projection script
│   └── run_backtest.py     walk-forward evaluation
├── tests/                  114 unit tests covering every numerical convention
├── data/
│   ├── acs_cache.json      cached ACS responses (committed; deterministic)
│   └── projections_*.json  generated projection outputs
└── backtests/results/      back-test reports (Markdown + CSV)
```

## Headline back-test result

96 folds (4 Hawaii counties × 4 indicators × 6 anchor years), 2-year
horizon, walk-forward:

| Method            | MAPE  | 90%-CI coverage |
|-------------------|------:|----------------:|
| carry-forward     | 8.91% |             29% |
| linear-log OLS    | 7.69% |             81% |
| damped log-trend  | 7.62% |             90% |
| AR(1) on log-diffs| 6.98% |             88% |
| **ensemble**      | **6.75%** |        **88%** |

## Hawaii 2026 projection (highlights)

ACS 2024 → 2026, ensemble model:

| Geography | Median HH income | Median gross rent | Median home value |
|-----------|-----------------:|------------------:|------------------:|
| Honolulu  | $105k → **$112k** | $2,001 → **$2,110** | $921k → **$987k** |
| Maui      | $101k → **$105k** | $1,944 → **$2,056** | $1.06M → **$1.17M** |
| Hawaii    |  $75k → **$80k**  | $1,645 → **$1,743** | $580k → **$632k** |
| Kauai     |  $98k → **$108k** | $1,866 → **$1,962** | $940k → **$1.03M** |

Full table with 90% CIs in
[`backtests/results/projection_2026_*.md`](backtests/results/).

## How it relates to the rest of this repo

The method shares discipline rules with the existing CPI/rent code:

* **Compound growth, not arithmetic.** Same convention as the
  `(1 + monthly_rate)^months_beyond` projection in
  `pipelines/grocery/src/price_adjuster.py`.
* **Per-period momentum cap.** ±10%/yr here is the annual analog of
  the ±0.0189/month cap that governs grocery and TFP projections.
* **Honest method tagging.** Every forecast row carries the model
  name, ensemble weights, and any cap/clamp note — same pattern as
  the `method ∈ {exact, interpolated, projected, unavailable}`
  field on `compute_cpi_ratio()`.
* **Walk-forward validation pattern.** Mirrors
  `backtests/rent_blend_walkforward.py` (Cleveland Fed style),
  including the cache layout for deterministic reruns.

The top-level [`METHODOLOGY.md`](../METHODOLOGY.md) governs the rest
of the dashboard's data pipeline; this folder's
[`METHODOLOGY.md`](METHODOLOGY.md) governs only the census forecasting
module.
