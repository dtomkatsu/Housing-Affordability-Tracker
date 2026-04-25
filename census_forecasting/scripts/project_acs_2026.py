#!/usr/bin/env python3
"""Project Hawaii ACS 2024 estimates forward to 2026.

Concrete production entry point: pulls the historical 1-year ACS panel,
runs the ensemble model, writes a JSON of point estimates + 90% CIs and
a Markdown report.

The four indicators projected match what the dashboard surfaces:
  B19013_001E — median household income (dollars)
  B25058_001E — median contract rent (dollars/month)
  B25064_001E — median gross rent (dollars/month)
  B25077_001E — median home value (dollars)

Outputs
-------
- `census_forecasting/data/projections_<run-date>.json`
- `census_forecasting/backtests/results/projection_<run-date>.md`

Run
---
    python3 census_forecasting/scripts/project_acs_2026.py
    python3 census_forecasting/scripts/project_acs_2026.py --target 2027
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from census_forecaster.acs.client import AcsClient
from census_forecaster.acs.anchors import load_calibration
from census_forecaster.acs.ensemble import project_ensemble, project_ensemble_multi
from census_forecaster.models import AcsObservation


HAWAII_FIPS = "15"
COUNTY_NAMES = {
    "15001": "Hawaii County",
    "15003": "Honolulu County",
    "15007": "Kauai County",
    "15009": "Maui County",
}

INDICATORS = {
    "B19013_001E": "median household income",
    "B25058_001E": "median contract rent",
    "B25064_001E": "median gross rent",
    "B25077_001E": "median home value",
}

TRAINING_YEARS = tuple(range(2010, 2025))


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


def run_projections(
    panel: dict[tuple[str, str], list[AcsObservation]],
    target_year: int,
    use_multi_anchor: bool = True,
) -> list[dict]:
    """Project every (geoid, indicator) pair forward to `target_year`.

    With `use_multi_anchor=True` (default), uses the multi-source anchor
    ensemble (`project_ensemble_multi`) — CPI / PCE / QCEW / HUD FMR /
    FHFA HPI per indicator, with weights and SE inflators sourced from
    `data/anchors/calibration.json`. Falls back to a trend-only
    ensemble for indicators with no admissible anchor sources.

    With `use_multi_anchor=False`, uses the legacy `project_ensemble`
    (no macro anchor — trend ensemble only).
    """
    calibration = load_calibration() if use_multi_anchor else None
    rows: list[dict] = []
    for (geoid, indicator), obs in sorted(panel.items()):
        obs_sorted = sorted(obs, key=lambda o: o.year)
        if not obs_sorted:
            continue
        latest = obs_sorted[-1]
        if use_multi_anchor:
            fp = project_ensemble_multi(
                obs_sorted,
                target_year=target_year,
                calibration=calibration,
            )
        else:
            fp = project_ensemble(obs_sorted, target_year=target_year)
        if fp is None:
            continue
        rows.append({
            "geoid": geoid,
            "geography": COUNTY_NAMES.get(geoid, geoid),
            "indicator": indicator,
            "indicator_label": INDICATORS[indicator],
            "anchor_year": latest.year,
            "anchor_value": latest.estimate,
            "anchor_moe": latest.moe,
            "target_year": target_year,
            "projected": round(fp.point, 2),
            "ci90_low": round(fp.ci90_low, 2),
            "ci90_high": round(fp.ci90_high, 2),
            "se_total": round(fp.se_total, 2),
            "se_sample": round(fp.se_sample, 2),
            "se_forecast": round(fp.se_forecast, 2),
            "method": fp.method,
            "horizon": fp.horizon,
            "notes": fp.notes,
            "implied_annual_growth_pct": round(
                ((fp.point / latest.estimate) ** (1.0 / fp.horizon) - 1.0) * 100,
                3,
            ) if fp.horizon > 0 else 0.0,
        })
    return rows


def write_report(
    rows: list[dict], target_year: int, out_dir: Path, run_date: str
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"projection_{target_year}_{run_date}.md"

    lines = []
    lines.append(f"# Hawaii ACS Projection: {target_year}\n\n")
    lines.append(f"**Run date:** {run_date}  \n")
    lines.append(
        f"**Method:** Multi-source anchor ensemble — damped log-trend + "
        f"AR(1) blended with calibrated CPI / PCE / QCEW / HUD FMR / "
        f"FHFA HPI anchors. Anchor weights and SE inflators sourced "
        f"from `data/anchors/calibration.json`. See METHODOLOGY.md.  \n"
    )
    lines.append(f"**Anchor vintage:** ACS 1-year, latest year per series\n\n")

    by_indicator = defaultdict(list)
    for r in rows:
        by_indicator[r["indicator"]].append(r)

    for ind in sorted(by_indicator.keys()):
        rs = sorted(by_indicator[ind], key=lambda x: x["geography"])
        label = INDICATORS.get(ind, ind)
        lines.append(f"## {ind} — {label}\n\n")
        lines.append(
            "| Geography | Anchor year | Anchor value | Projected "
            f"({target_year}) | CI90 low | CI90 high | CAGR | Notes |\n"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---|\n")
        for r in rs:
            lines.append(
                f"| {r['geography']} | {r['anchor_year']} | "
                f"${r['anchor_value']:,.0f} | "
                f"**${r['projected']:,.0f}** | "
                f"${r['ci90_low']:,.0f} | ${r['ci90_high']:,.0f} | "
                f"{r['implied_annual_growth_pct']:+.2f}% | "
                f"{r['notes'][:80]} |\n"
            )
        lines.append("\n")

    lines.append("## Method notes\n\n")
    lines.append(
        "* Point estimates are the multi-source anchor ensemble: a trend "
        "ensemble (damped log-trend + AR(1)) blended with a calibrated "
        "macro anchor combining CPI Honolulu, PCE deflator, QCEW Hawaii "
        "wages, HUD FMR Honolulu, and FHFA HPI Hawaii — each weighted by "
        "its inverse-RMSE on hold-out folds. Macro/trend blend weight is "
        "the Bates-Granger optimum: `RMSE_trend²/(RMSE_trend²+RMSE_macro²)`.\n"
        "* 90% CIs combine ACS sample standard error (MOE/1.645), model "
        "forecast SE (Hyndman ETS(A,Ad,N) closed-form), anchor-rate "
        "uncertainty (calibration-derived per-source SE), and a "
        "per-(indicator, method) empirical SE inflator that brings "
        "back-test 90%-CI coverage into [85%, 95%].\n"
        "* Projections are capped at ±10%/yr compound growth — the "
        "annual analog of the ±0.0189/month CPI cap that governs the "
        "rest of this repo.\n"
        "* Hold-out back-tests on 2015-2022 anchors (96 folds, 4 "
        "counties × 4 indicators × 6 anchors, 2-year horizon) — "
        "see `backtests/results/backtest_*.md` and `calibration_*.md` "
        "for per-indicator metrics.\n"
    )

    report.write_text("".join(lines))
    return report


def write_json(rows: list[dict], target_year: int, out_dir: Path, run_date: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "run_date": run_date,
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "target_year": target_year,
        "method": "ensemble_multi_anchor (damped_log_trend + ar1_log_diff + multi-source macro)",
        "rows": rows,
    }
    out = out_dir / f"projections_{target_year}_{run_date}.json"
    out.write_text(json.dumps(payload, indent=2))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--target", type=int, default=2026,
                   help="Target year (default 2026)")
    p.add_argument("--offline", action="store_true")
    args = p.parse_args()

    cache_path = ROOT / "census_forecasting" / "data" / "acs_cache.json"
    client = AcsClient(cache_path=cache_path, offline=args.offline)
    panel = fetch_panel(client)
    print(f"Fetched {sum(len(v) for v in panel.values())} observations across "
          f"{len(panel)} (geoid, indicator) pairs.")

    rows = run_projections(panel, target_year=args.target)
    run_date = date.today().isoformat()

    json_path = write_json(rows, args.target, ROOT / "census_forecasting" / "data", run_date)
    md_path = write_report(rows, args.target, ROOT / "census_forecasting" / "backtests" / "results", run_date)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")

    print()
    for r in rows:
        if r["geoid"] == "15003" and r["indicator"] == "B19013_001E":
            print(
                f"Honolulu median HH income {r['anchor_year']} → {args.target}: "
                f"${r['anchor_value']:,.0f} → ${r['projected']:,.0f} "
                f"(90% CI ${r['ci90_low']:,.0f}-${r['ci90_high']:,.0f}, "
                f"CAGR {r['implied_annual_growth_pct']:+.2f}%)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
