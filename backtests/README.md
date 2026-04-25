# Backtests

Pseudo-out-of-sample evaluation harnesses for the dashboard's projection logic.
Methodology follows Cleveland Fed WP 22-38r — at each anchor date `T`, only data
available at `T` is used to produce a projection, then compared to a realized
ground-truth series at `T+12`.

## Harnesses

### `rent_blend_walkforward.py`

Walk-forward backtest of the BLS-CPI / ZORI rent blend driving
`redfin-price-updater.py::blend_rent_nowcast()`.

For each anchor `T ∈ {2022-04, 2022-10, 2023-04, 2023-10, 2024-04}`:

1. Pull BLS rent CPI (`CUURS49ASEHA`) capped at `T`
2. Pull ZORI capped at `T`
3. Pick ACS 5-year vintage available at `T` (release lag ~Dec of year+2)
4. Run `blend_rent_nowcast()` for the live 70/30 weight + comparison baselines
   (BLS-only, ZORI-only, 50/50, 60/40)
5. Compare to ground truth at `T+12` = average of BLS dollars + ZORI dollars,
   each scaled from the same ACS anchor

Output: `results/rent_blend_<run-date>.md` with a per-county / per-weight error
table + recommendation paragraph. The live `BLENDED_RENT_CPI_WEIGHT = 0.7`
constant is **not** auto-modified by this harness.

#### Run

```bash
python3 backtests/rent_blend_walkforward.py            # cold cache: ~2 min
python3 backtests/rent_blend_walkforward.py --no-cache # bypass cache
```

#### Cache

BLS API responses cached to `cache/bls_<series>.json` (full history, then sliced
per anchor in-memory). ZORI is a single full-history CSV cached to
`cache/zori_county.csv`. ACS historical pulls are per-vintage and cached as
`cache/acs_<vintage>.json`.

The cache exists to (a) avoid re-hitting the BLS public-API daily quota on
reruns, and (b) make the harness deterministic. Delete `cache/` to force a
fresh fetch.

## Refresh cadence

Re-run after each annual ACS 5-year vintage release (typically December).
Update the anchor list and target metric to include the most recent year.
