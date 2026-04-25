#!/usr/bin/env python3
"""Calibrate multi-source anchor weights and SE inflators on hold-out data.

Two-pass back-test:
1. Per-source RMSE — feeds inverse-variance weights inside the multi-anchor combiner.
2. Per-method RMSE + CI coverage — feeds the macro/trend blend weight
   and the per-(indicator, method) SE inflator override.

Output: `census_forecasting/data/anchors/calibration.json`
        `census_forecasting/backtests/results/calibration_<run-date>.md`

Run
---
    python3 census_forecasting/scripts/calibrate_anchors.py
    python3 census_forecasting/scripts/calibrate_anchors.py --horizon 2
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from census_forecaster.acs.client import AcsClient
from census_forecaster.acs.calibration import (
    run_holdout_calibration,
    write_calibration,
    COVERAGE_LOWER_BOUND,
    COVERAGE_UPPER_BOUND,
)
from census_forecaster.models import AcsObservation


HAWAII_FIPS = "15"
INDICATORS = [
    "B19013_001E",
    "B25058_001E",
    "B25064_001E",
    "B25077_001E",
]
TRAINING_YEARS = tuple(range(2010, 2025))
ANCHOR_YEARS = [2015, 2016, 2017, 2019, 2021, 2022]


def fetch_panel(client: AcsClient) -> dict[tuple[str, str], list[AcsObservation]]:
    panel: dict[tuple[str, str], list[AcsObservation]] = defaultdict(list)
    for indicator in INDICATORS:
        observations = client.fetch_series(
            indicator=indicator,
            years=TRAINING_YEARS,
            vintage="1y",
            state_fips=HAWAII_FIPS,
            county_fips=None,
        )
        for o in observations:
            panel[(o.geoid, o.indicator)].append(o)
    return panel


def write_report(
    payload: dict, out_dir: Path, run_date: str
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"calibration_{run_date}.md"

    lines = []
    lines.append(f"# Anchor calibration\n")
    lines.append(f"**Run date:** {run_date}\n")
    lines.append(f"**Anchor years:** {payload['anchor_years']}\n")
    lines.append(f"**Horizon:** {payload['horizon']}y\n")
    lines.append(f"**Coverage band:** [{COVERAGE_LOWER_BOUND * 100:.0f}%, {COVERAGE_UPPER_BOUND * 100:.0f}%]\n\n")

    lines.append("## Per-(indicator, source) RMSE\n\n")
    lines.append("Lower RMSE → higher weight in the multi-source anchor combiner.\n\n")
    src_table = payload.get("rmse_by_indicator_source", {})
    for ind in sorted(src_table.keys()):
        lines.append(f"### {ind}\n\n")
        lines.append("| Source | RMSE (pct error) |\n|---|---:|\n")
        for src, rmse in sorted(src_table[ind].items(), key=lambda x: x[1]):
            lines.append(f"| {src} | {rmse * 100:.2f}% |\n")
        lines.append("\n")

    lines.append("## Per-(indicator, method) RMSE + CI90 coverage\n\n")
    rmse_m = payload.get("rmse_by_indicator_method", {})
    cov_m = payload.get("ci90_coverage_by_indicator_method", {})
    for ind in sorted(rmse_m.keys()):
        lines.append(f"### {ind}\n\n")
        lines.append("| Method | RMSE | CI90 coverage |\n|---|---:|---:|\n")
        for m in sorted(rmse_m[ind].keys()):
            r = rmse_m[ind][m]
            c = cov_m.get(ind, {}).get(m, float("nan"))
            r_str = f"{r * 100:.2f}%" if r is not None and r == r else "—"
            c_str = f"{c * 100:.1f}%" if c is not None and c == c else "—"
            lines.append(f"| {m} | {r_str} | {c_str} |\n")
        lines.append("\n")

    overrides = payload.get("se_inflator_override_by_indicator_method", {})
    if overrides:
        lines.append("## SE inflator overrides (where coverage outside [85%, 95%])\n\n")
        lines.append("| Indicator | Method | Override factor |\n|---|---|---:|\n")
        for ind in sorted(overrides.keys()):
            for m, val in sorted(overrides[ind].items()):
                lines.append(f"| {ind} | {m} | {val:.3f} |\n")
        lines.append("\n")
    else:
        lines.append("All per-(indicator, method) coverage already inside [85%, 95%]; no overrides applied.\n\n")

    md_path.write_text("".join(lines))
    return md_path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--horizon", type=int, default=2)
    p.add_argument("--offline", action="store_true")
    args = p.parse_args()

    cache_path = ROOT / "census_forecasting" / "data" / "acs_cache.json"
    client = AcsClient(cache_path=cache_path, offline=args.offline)
    panel = fetch_panel(client)
    if not panel:
        print("[calibrate_anchors] No ACS observations available — exiting.", file=sys.stderr)
        return 1

    payload = run_holdout_calibration(
        series_by_key=panel,
        anchor_years=ANCHOR_YEARS,
        horizon=args.horizon,
    )

    calib_path = ROOT / "census_forecasting" / "data" / "anchors" / "calibration.json"
    write_calibration(payload, calib_path)
    md = write_report(payload, ROOT / "census_forecasting" / "backtests" / "results", date.today().isoformat())
    print(f"Wrote {calib_path}")
    print(f"Wrote {md}")

    print()
    rmse_m = payload.get("rmse_by_indicator_method", {})
    for ind in sorted(rmse_m.keys()):
        for m in sorted(rmse_m[ind].keys()):
            cov = payload["ci90_coverage_by_indicator_method"].get(ind, {}).get(m, float("nan"))
            print(f"  {ind:14s}  {m:18s}  RMSE {rmse_m[ind][m] * 100:5.2f}%  cov {cov * 100:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
