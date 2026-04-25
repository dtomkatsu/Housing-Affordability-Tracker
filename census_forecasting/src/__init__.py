"""Internal modules for census forecasting.

- `moe`         — MOE → SE conversion + Census-handbook propagation formulas
- `acs_client`  — ACS public API client with on-disk JSON cache
- `models`      — typed dataclasses for observations and forecasts
- `projection`  — damped local linear trend in log space (the workhorse)
- `ensemble`    — combine candidate models with macro anchors
- `anchors`     — multi-source macro-anchor combiner (CPI / PCE / QCEW / HUD / FHFA)
- `sources`     — embedded historical anchor series with publication-lag handling
- `calibration` — hidden-data hold-out calibration of weights + SE inflators
- `backtest`    — walk-forward pseudo-out-of-sample evaluation
"""
