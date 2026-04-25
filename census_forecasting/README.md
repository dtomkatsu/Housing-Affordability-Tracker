# census_forecasting

Project forward ACS census estimates with calibrated uncertainty.
Hawaii-focused defaults; the method generalises to any state/county
ACS panel.

## What this gives you

* **Point estimates and 90% prediction intervals** for ACS indicators
  at horizons of 1-5 years, defaulting to ACS 2024 → 2026.
* A **multi-source-anchor ensemble** — damped log-trend + AR(1)
  blended with calibrated CPI / PCE / QCEW / HUD FMR / FHFA HPI
  anchors, weights derived from out-of-sample RMSE. Beats every
  alternative on every metric in the 96-fold walk-forward back-test
  (MAPE 6.16%, CI cov 90%).
* **MOE propagation** through the forecast — sample uncertainty from
  the ACS-published margin of error fused with model forecast
  variance and (calibrated) anchor-rate uncertainty.
* **Hidden-data hold-out calibration** that derives per-source
  weights, per-(indicator, method) RMSE, and per-(indicator, method)
  SE inflators — all from out-of-sample folds, no peeking. Coverage
  is verified post-override to sit inside [85%, 95%] for every cell.
* **Reproducibility** — cached ACS responses + embedded anchor series
  are committed, all numbers pinned in tests, the calibration is
  re-derivable in seconds offline.

See [METHODOLOGY.md](METHODOLOGY.md) for the full design rationale,
references, and limitations.

## Quick start

```bash
# Run the test suite (offline; uses the committed cache)
python3 -m pytest census_forecasting/tests/ -v

# Re-derive the multi-source anchor calibration (writes data/anchors/calibration.json)
python3 census_forecasting/scripts/calibrate_anchors.py

# Generate Hawaii 2026 projections (4 counties × 4 indicators)
python3 census_forecasting/scripts/project_acs_2026.py

# Re-run the walk-forward back-test
python3 census_forecasting/scripts/run_backtest.py
```

## Layout

```
census_forecasting/
├── METHODOLOGY.md            design rationale, references, limitations
├── README.md                 this file
├── src/
│   ├── moe.py                MOE → SE conversion + Census Handbook propagation
│   ├── models.py             AcsObservation / GeographySeries / ForecastPoint
│   ├── acs_client.py         ACS API client with on-disk cache
│   ├── projection.py         damped log-trend + AR(1) on log-diffs
│   ├── ensemble.py           inverse-variance combiner + macro anchors
│   ├── anchors.py            multi-source macro-anchor combiner
│   ├── sources/              per-source loaders (CPI, PCE, QCEW, HUD, FHFA)
│   ├── calibration.py        hold-out calibration of weights + SE inflators
│   └── backtest.py           walk-forward harness with naive baselines
├── scripts/
│   ├── calibrate_anchors.py  hold-out calibration writer
│   ├── project_acs_2026.py   production projection script
│   └── run_backtest.py       walk-forward evaluation
├── tests/                    170 unit + stress tests
├── data/
│   ├── acs_cache.json        cached ACS responses (committed; deterministic)
│   ├── anchors/*.json        embedded historical anchor series + calibration
│   └── projections_*.json    generated projection outputs
└── backtests/results/        back-test + calibration reports (Markdown + CSV)
```

## Headline back-test result

96 folds (4 Hawaii counties × 4 indicators × 6 anchor years), 2-year
horizon, walk-forward:

| Method                         | MAPE  | RMSE-pct | 90%-CI cov |
|--------------------------------|------:|---------:|-----------:|
| carry-forward                  | 8.91% |   10.68% |        29% |
| linear-log OLS                 | 7.69% |    9.64% |        81% |
| damped log-trend               | 7.64% |    9.45% |        93% |
| AR(1) on log-diffs             | 6.98% |    8.81% |        88% |
| ensemble (trend only)          | 6.76% |    8.73% |        89% |
| **ensemble + multi-anchor**    | **6.16%** | **7.96%** |    **90%** |

## Hawaii 2026 projection (highlights)

ACS 2024 → 2026, multi-anchor ensemble:

| Geography | Median HH income | Median gross rent | Median home value |
|-----------|-----------------:|------------------:|------------------:|
| Honolulu  | $105k → **$113k** | $2,001 → **$2,131** | $921k → **$999k** |
| Maui      | $101k → **$107k** | $1,944 → **$2,075** | $1.06M → **$1.18M** |
| Hawaii    |  $75k → **$81k**  | $1,645 → **$1,759** | $580k → **$638k** |
| Kauai     |  $98k → **$107k** | $1,866 → **$1,984** | $940k → **$1.04M** |

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
