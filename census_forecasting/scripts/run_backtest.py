#!/usr/bin/env python3
"""Walk-forward back-test of ACS projection methods on Hawaii counties.

Fetches the historical 1-year ACS series for Hawaii's four counties
across four indicators (median HH income, median contract rent, median
gross rent, median home value), then runs the model ensemble plus
naive baselines at each anchor year T ∈ {2015, ..., 2022} projecting to
T + 2.

Output: `census_forecasting/backtests/results/backtest_<run-date>.md`
plus a CSV of every fold for downstream slicing.

Run
---
    python3 census_forecasting/scripts/run_backtest.py
    python3 census_forecasting/scripts/run_backtest.py --no-cache  # bust cache
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

# Allow running as a module from repo root or as a script.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from census_forecaster.acs.client import AcsClient
from census_forecaster.acs.anchors import load_calibration
from census_forecaster.backtest.acs import (
    DEFAULT_METHODS,
    make_methods_with_multi_anchor,
    run_backtest,
)
from census_forecaster.models import AcsObservation


HAWAII_FIPS = "15"
HAWAII_COUNTIES = ["001", "003", "007", "009"]  # Hawaii, Honolulu, Kauai, Maui

INDICATORS = [
    ("B19013_001E", "median household income"),
    ("B25058_001E", "median contract rent"),
    ("B25064_001E", "median gross rent"),
    ("B25077_001E", "median home value"),
]

# ACS 1y vintages we'll fetch. 2020 is suspended (COVID), and the client
# returns [] for that year. The back-test drops missing observations
# gracefully so the gap doesn't break anything.
TRAINING_YEARS = tuple(range(2010, 2025))

# Anchor years to evaluate. T+2 must be a year with 1y data (≠ 2020),
# so anchor must not equal 2018 (T+2 = 2020 missing). 2022 is the
# latest anchor that gives a 2-year-out comparable (T+2 = 2024).
ANCHOR_YEARS = [2015, 2016, 2017, 2019, 2021, 2022]


def fetch_hawaii_series(client: AcsClient) -> dict[tuple[str, str], list[AcsObservation]]:
    """Pull all (county × indicator) 1y series for the years above."""
    series_by_key: dict[tuple[str, str], list[AcsObservation]] = defaultdict(list)
    for indicator, _label in INDICATORS:
        observations = client.fetch_series(
            indicator=indicator,
            years=TRAINING_YEARS,
            vintage="1y",
            state_fips=HAWAII_FIPS,
            county_fips=None,  # all counties at once
        )
        for o in observations:
            series_by_key[(o.geoid, o.indicator)].append(o)
    return series_by_key


def write_report(
    summaries, all_rows, target_year_horizon, run_date: str, out_dir: Path
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"backtest_{run_date}.md"
    csv_path = out_dir / f"backtest_{run_date}.csv"

    method_order = [
        "carry_forward", "linear_log", "damped_log_trend", "ar1_log_diff",
        "ensemble", "ensemble_multi_anchor",
    ]

    lines = []
    lines.append(f"# ACS Projection Walk-Forward Back-Test\n")
    lines.append(f"**Run date:** {run_date}\n")
    lines.append(f"**Horizon:** {target_year_horizon} years (each fold projects T → T+{target_year_horizon})\n")
    lines.append(f"**Geographies:** Hawaii's 4 counties (FIPS 15001 / 15003 / 15007 / 15009)\n")
    lines.append(f"**Indicators:** B19013 (median HH income), B25058 (median contract rent), B25064 (median gross rent), B25077 (median home value)\n")
    lines.append(f"**Anchor years:** {ANCHOR_YEARS}\n\n")

    lines.append("## Aggregate metrics\n")
    lines.append("| Method | n | MAPE | medAPE | RMSE-pct | Bias | CI90 cov |\n")
    lines.append("|---|---:|---:|---:|---:|---:|---:|\n")
    for m in method_order:
        s = summaries.get(m)
        if s is None or s.n == 0:
            lines.append(f"| {m} | 0 | – | – | – | – | – |\n")
            continue
        lines.append(
            f"| {m} | {s.n} | "
            f"{s.mean_abs_pct_error * 100:.2f}% | "
            f"{s.median_abs_pct_error * 100:.2f}% | "
            f"{s.rmse_pct * 100:.2f}% | "
            f"{s.bias_pct * 100:+.2f}% | "
            f"{s.ci90_coverage * 100:.1f}% |\n"
        )

    # Per-indicator breakdown for the ensemble (the production model).
    lines.append("\n## Ensemble per-indicator breakdown\n")
    lines.append("| Indicator | n | MAPE | medAPE | Bias | CI90 cov |\n")
    lines.append("|---|---:|---:|---:|---:|---:|\n")
    by_ind: dict[str, list] = defaultdict(list)
    for r in summaries.get("ensemble", type("X", (), {"rows": []})()).rows:
        by_ind[r.indicator].append(r)
    for ind, rows in sorted(by_ind.items()):
        if not rows:
            continue
        pct_errs = [(r.projected - r.actual) / r.actual for r in rows]
        abs_p = [abs(e) for e in pct_errs]
        cov = sum(1 for r in rows if r.ci90_low <= r.actual <= r.ci90_high) / len(rows)
        lines.append(
            f"| {ind} | {len(rows)} | "
            f"{sum(abs_p) / len(abs_p) * 100:.2f}% | "
            f"{sorted(abs_p)[len(abs_p) // 2] * 100:.2f}% | "
            f"{sum(pct_errs) / len(pct_errs) * 100:+.2f}% | "
            f"{cov * 100:.1f}% |\n"
        )

    lines.append("\n## Reading the table\n")
    lines.append(
        "* **MAPE / medAPE**: lower is better. The ensemble should beat or "
        "match `carry_forward` and `linear_log` to justify its complexity.\n"
        "* **Bias**: signed mean of pct error. >0 means the model "
        "systematically over-projects; <0 means under-projects.\n"
        "* **CI90 cov**: fraction of folds whose actual fell inside the "
        "projected 90% interval. ~90% is well-calibrated; >>90% means "
        "intervals are wider than necessary; <<90% means intervals "
        "understate uncertainty.\n"
    )

    report.write_text("".join(lines))

    # Also dump the full per-fold CSV.
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "method", "geoid", "indicator", "anchor_year", "target_year",
            "horizon", "actual", "projected", "ci90_low", "ci90_high",
            "sample_se", "forecast_se", "abs_pct_err", "in_ci",
        ])
        for s in summaries.values():
            for r in s.rows:
                ape = abs((r.projected - r.actual) / r.actual)
                in_ci = 1 if r.ci90_low <= r.actual <= r.ci90_high else 0
                w.writerow([
                    r.method, r.geoid, r.indicator, r.anchor_year, r.target_year,
                    r.horizon, f"{r.actual:.2f}", f"{r.projected:.2f}",
                    f"{r.ci90_low:.2f}", f"{r.ci90_high:.2f}",
                    f"{r.sample_se:.2f}", f"{r.forecast_se:.2f}",
                    f"{ape:.6f}", in_ci,
                ])
    return report


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--no-cache", action="store_true",
                   help="Force fresh ACS fetches (ignores on-disk cache).")
    p.add_argument("--horizon", type=int, default=2)
    p.add_argument("--offline", action="store_true",
                   help="Use only on-disk cache; never hit the network.")
    args = p.parse_args()

    cache_path = ROOT / "census_forecasting" / "data" / "acs_cache.json"
    if args.no_cache and cache_path.exists():
        cache_path.unlink()

    client = AcsClient(cache_path=cache_path, offline=args.offline)
    series = fetch_hawaii_series(client)

    print(f"Fetched {sum(len(v) for v in series.values())} observations across "
          f"{len(series)} (geoid, indicator) pairs.")

    calibration = load_calibration()
    methods = make_methods_with_multi_anchor(calibration=calibration)
    summaries = run_backtest(
        series_by_key=series,
        anchors=ANCHOR_YEARS,
        horizon=args.horizon,
        methods=methods,
    )

    print("\n=== Aggregate ===\n")
    for m in [
        "carry_forward", "linear_log", "damped_log_trend",
        "ar1_log_diff", "ensemble", "ensemble_multi_anchor",
    ]:
        s = summaries.get(m)
        if s is None:
            continue
        print(s)

    out_dir = ROOT / "census_forecasting" / "backtests" / "results"
    run_date = date.today().isoformat()
    report = write_report(summaries, None, args.horizon, run_date, out_dir)
    print(f"\nWrote {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
