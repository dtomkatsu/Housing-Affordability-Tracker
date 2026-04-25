#!/usr/bin/env python3
"""Hawaii-tracker driver for the Census-Forecaster CPI walk-forward harness.

The actual walk-forward logic — sweep grid, anchor enumeration, gap-weighted
variant, calibration mode — lives in `census_forecaster.backtest.cpi`. This
file is a thin convenience entry point that:

* Sets `CENSUS_FORECASTER_BACKTEST_DIR` so the harness writes its cached
  BLS pulls and Markdown report to `backtests/cache/cpi/` and
  `backtests/results/` inside this repo (rather than the package's own
  default of `~/.cache/census-forecaster/`).
* Delegates argument parsing and execution to the package.

Run
~~~
    python3 backtests/cpi_projection_walkforward.py
    python3 backtests/cpi_projection_walkforward.py --calibrate
    python3 backtests/cpi_projection_walkforward.py --variant compare

See `census_forecaster/src/census_forecaster/backtest/cpi.py` for the
harness internals and the full list of CLI options.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Anchor backtest output to this repo, not the user-cache dir.
ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault(
    "CENSUS_FORECASTER_BACKTEST_DIR",
    str(ROOT / "backtests"),
)

from census_forecaster.backtest.cpi import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
