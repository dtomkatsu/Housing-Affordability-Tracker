# Rent-blend walk-forward backtest — 2026-04-25

Pseudo-out-of-sample evaluation of `blend_rent_nowcast()` (live weight 0.70 CPI / 0.30 ZORI). For each anchor T, we form a blended rent estimate using only data available at T, then compare to two ground-truth proxies at T+12:

1. **Blend-truth** = (BLS-dollars + ZORI-dollars) / 2 at T+12 — the construction in the original plan; biased toward whichever input is more current at T+12 (ZORI).
2. **BLS-only-truth** = BLS-dollars at T+12 — leverages the BLS ~12-month lag so BLS at T+12 ≈ rent at T; biased toward CPI-heavy weights but more directly addresses the nowcast question.

Both proxies share the same ACS vintage and base-year scaling, so dollar values are directly comparable to the prediction.

## Ground truth A — Blend ((BLS+ZORI)/2)

### Detail vs ground truth = (BLS+ZORI)/2

| Anchor | T+12 | ACS vint. | Region | Anchor $ | BLS-only | 70/30 | 60/40 | 50/50 | ZORI-only | Realized | |70/30 err| | %err 70/30 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2022-04 | 2023-04 | 2020 | State | $1,497 | $1,564 | $1,608 | $1,623 | $1,638 | $1,712 | $1,709 | $101 | -5.90% |
| 2022-04 | 2023-04 | 2020 | Honolulu | $1,638 | $1,711 | $1,759 | $1,775 | $1,791 | $1,872 | $1,869 | $110 | -5.86% |
| 2022-04 | 2023-04 | 2020 | Hawaii | $1,053 | $1,100 | $1,131 | $1,142 | $1,152 | $1,204 | $1,202 | $71 | -5.91% |
| 2022-04 | 2023-04 | 2020 | Maui | $1,395 | $1,457 | $1,601 | $1,648 | $1,696 | $1,935 | $1,760 | $159 | -9.04% |
| 2022-04 | 2023-04 | 2020 | Kauai | $1,249 | $1,305 | $1,342 | $1,354 | $1,366 | $1,428 | $1,426 | $84 | -5.88% |
| 2022-10 | 2023-10 | 2020 | State | $1,497 | $1,606 | $1,640 | $1,652 | $1,663 | $1,720 | $1,751 | $111 | -6.33% |
| 2022-10 | 2023-10 | 2020 | Honolulu | $1,638 | $1,757 | $1,797 | $1,810 | $1,823 | $1,889 | $1,906 | $109 | -5.70% |
| 2022-10 | 2023-10 | 2020 | Hawaii | $1,053 | $1,130 | $1,154 | $1,162 | $1,170 | $1,210 | $1,232 | $78 | -6.30% |
| 2022-10 | 2023-10 | 2020 | Maui | $1,395 | $1,497 | $1,616 | $1,656 | $1,696 | $1,895 | $1,833 | $217 | -11.83% |
| 2022-10 | 2023-10 | 2020 | Kauai | $1,249 | $1,340 | $1,369 | $1,378 | $1,387 | $1,435 | $1,461 | $92 | -6.28% |
| 2023-04 | 2024-04 | 2021 | State | $1,591 | $1,730 | $1,738 | $1,741 | $1,743 | $1,757 | $1,836 | $98 | -5.35% |
| 2023-04 | 2024-04 | 2021 | Honolulu | $1,720 | $1,870 | $1,879 | $1,882 | $1,885 | $1,900 | $1,964 | $85 | -4.31% |
| 2023-04 | 2024-04 | 2021 | Hawaii | $1,086 | $1,181 | $1,199 | $1,205 | $1,211 | $1,242 | $1,297 | $98 | -7.59% |
| 2023-04 | 2024-04 | 2021 | Maui | $1,497 | $1,628 | $1,670 | $1,684 | $1,698 | $1,769 | $1,874 | $204 | -10.88% |
| 2023-04 | 2024-04 | 2021 | Kauai | $1,352 | $1,470 | $1,477 | $1,479 | $1,482 | $1,493 | $1,560 | $83 | -5.35% |
| 2023-10 | 2024-10 | 2021 | State | $1,591 | $1,775 | $1,782 | $1,784 | $1,786 | $1,797 | $1,864 | $82 | -4.38% |
| 2023-10 | 2024-10 | 2021 | Honolulu | $1,720 | $1,919 | $1,921 | $1,922 | $1,923 | $1,927 | $2,011 | $90 | -4.48% |
| 2023-10 | 2024-10 | 2021 | Hawaii | $1,086 | $1,212 | $1,237 | $1,246 | $1,255 | $1,297 | $1,304 | $67 | -5.16% |
| 2023-10 | 2024-10 | 2021 | Maui | $1,497 | $1,670 | $1,728 | $1,747 | $1,766 | $1,862 | $1,824 | $96 | -5.27% |
| 2023-10 | 2024-10 | 2021 | Kauai | $1,352 | $1,509 | $1,514 | $1,516 | $1,518 | $1,527 | $1,584 | $70 | -4.40% |
| 2024-04 | 2025-04 | 2022 | State | $1,704 | $1,864 | $1,864 | $1,864 | $1,864 | $1,863 | $1,942 | $78 | -4.02% |
| 2024-04 | 2025-04 | 2022 | Honolulu | $1,824 | $1,996 | $1,981 | $1,977 | $1,972 | $1,948 | $2,058 | $77 | -3.76% |
| 2024-04 | 2025-04 | 2022 | Hawaii | $1,160 | $1,269 | $1,284 | $1,289 | $1,294 | $1,319 | $1,342 | $58 | -4.32% |
| 2024-04 | 2025-04 | 2022 | Maui | $1,614 | $1,766 | $1,814 | $1,830 | $1,846 | $1,926 | $1,839 | $25 | -1.35% |
| 2024-04 | 2025-04 | 2022 | Kauai | $1,498 | $1,639 | $1,639 | $1,639 | $1,638 | $1,638 | $1,707 | $68 | -4.00% |

### Aggregate vs ground truth = (BLS+ZORI)/2

| Weight scheme | N | MAE ($) | MAPE | Max abs err ($) |
|---|---|---|---|---|
| BLS-only | 25 | 128 | 7.62% | 336 |
| 70/30 (live) | 25 | 96 | 5.75% | 217 |
| 60/40 | 25 | 86 | 5.12% | 190 |
| 50/50 | 25 | 76 | 4.53% | 176 |
| ZORI-only | 25 | 53 | 3.03% | 175 |

### Per-region MAPE vs ground truth = (BLS+ZORI)/2 (live 70/30 weight)

| Region | N anchors | MAPE | MAE ($) |
|---|---|---|---|
| State | 5 | 5.20% | 94 |
| Honolulu | 5 | 4.82% | 94 |
| Hawaii | 5 | 5.85% | 74 |
| Maui | 5 | 7.67% | 140 |
| Kauai | 5 | 5.18% | 79 |

## Ground truth B — BLS-only (BLS at T+12 ≈ rent at T)

### Detail vs ground truth = BLS at T+12

| Anchor | T+12 | ACS vint. | Region | Anchor $ | BLS-only | 70/30 | 60/40 | 50/50 | ZORI-only | Realized | |70/30 err| | %err 70/30 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2022-04 | 2023-04 | 2020 | State | $1,497 | $1,564 | $1,608 | $1,623 | $1,638 | $1,712 | $1,648 | $40 | -2.40% |
| 2022-04 | 2023-04 | 2020 | Honolulu | $1,638 | $1,711 | $1,759 | $1,775 | $1,791 | $1,872 | $1,803 | $44 | -2.43% |
| 2022-04 | 2023-04 | 2020 | Hawaii | $1,053 | $1,100 | $1,131 | $1,142 | $1,152 | $1,204 | $1,159 | $28 | -2.41% |
| 2022-04 | 2023-04 | 2020 | Maui | $1,395 | $1,457 | $1,601 | $1,648 | $1,696 | $1,935 | $1,535 | $66 | +4.28% |
| 2022-04 | 2023-04 | 2020 | Kauai | $1,249 | $1,305 | $1,342 | $1,354 | $1,366 | $1,428 | $1,375 | $33 | -2.37% |
| 2022-10 | 2023-10 | 2020 | State | $1,497 | $1,606 | $1,640 | $1,652 | $1,663 | $1,720 | $1,691 | $51 | -3.01% |
| 2022-10 | 2023-10 | 2020 | Honolulu | $1,638 | $1,757 | $1,797 | $1,810 | $1,823 | $1,889 | $1,850 | $53 | -2.88% |
| 2022-10 | 2023-10 | 2020 | Hawaii | $1,053 | $1,130 | $1,154 | $1,162 | $1,170 | $1,210 | $1,189 | $35 | -2.98% |
| 2022-10 | 2023-10 | 2020 | Maui | $1,395 | $1,497 | $1,616 | $1,656 | $1,696 | $1,895 | $1,576 | $40 | +2.56% |
| 2022-10 | 2023-10 | 2020 | Kauai | $1,249 | $1,340 | $1,369 | $1,378 | $1,387 | $1,435 | $1,411 | $42 | -2.96% |
| 2023-04 | 2024-04 | 2021 | State | $1,591 | $1,730 | $1,738 | $1,741 | $1,743 | $1,757 | $1,817 | $79 | -4.33% |
| 2023-04 | 2024-04 | 2021 | Honolulu | $1,720 | $1,870 | $1,879 | $1,882 | $1,885 | $1,900 | $1,964 | $85 | -4.33% |
| 2023-04 | 2024-04 | 2021 | Hawaii | $1,086 | $1,181 | $1,199 | $1,205 | $1,211 | $1,242 | $1,240 | $41 | -3.31% |
| 2023-04 | 2024-04 | 2021 | Maui | $1,497 | $1,628 | $1,670 | $1,684 | $1,698 | $1,769 | $1,709 | $39 | -2.30% |
| 2023-04 | 2024-04 | 2021 | Kauai | $1,352 | $1,470 | $1,477 | $1,479 | $1,482 | $1,493 | $1,544 | $67 | -4.32% |
| 2023-10 | 2024-10 | 2021 | State | $1,591 | $1,775 | $1,782 | $1,784 | $1,786 | $1,797 | $1,859 | $77 | -4.15% |
| 2023-10 | 2024-10 | 2021 | Honolulu | $1,720 | $1,919 | $1,921 | $1,922 | $1,923 | $1,927 | $2,010 | $89 | -4.42% |
| 2023-10 | 2024-10 | 2021 | Hawaii | $1,086 | $1,212 | $1,237 | $1,246 | $1,255 | $1,297 | $1,269 | $32 | -2.52% |
| 2023-10 | 2024-10 | 2021 | Maui | $1,497 | $1,670 | $1,728 | $1,747 | $1,766 | $1,862 | $1,749 | $21 | -1.21% |
| 2023-10 | 2024-10 | 2021 | Kauai | $1,352 | $1,509 | $1,514 | $1,516 | $1,518 | $1,527 | $1,580 | $66 | -4.17% |
| 2024-04 | 2025-04 | 2022 | State | $1,704 | $1,864 | $1,864 | $1,864 | $1,864 | $1,863 | $1,949 | $85 | -4.38% |
| 2024-04 | 2025-04 | 2022 | Honolulu | $1,824 | $1,996 | $1,981 | $1,977 | $1,972 | $1,948 | $2,087 | $106 | -5.06% |
| 2024-04 | 2025-04 | 2022 | Hawaii | $1,160 | $1,269 | $1,284 | $1,289 | $1,294 | $1,319 | $1,327 | $43 | -3.24% |
| 2024-04 | 2025-04 | 2022 | Maui | $1,614 | $1,766 | $1,814 | $1,830 | $1,846 | $1,926 | $1,846 | $32 | -1.75% |
| 2024-04 | 2025-04 | 2022 | Kauai | $1,498 | $1,639 | $1,639 | $1,639 | $1,638 | $1,638 | $1,714 | $75 | -4.36% |

### Aggregate vs ground truth = BLS at T+12

| Weight scheme | N | MAE ($) | MAPE | Max abs err ($) |
|---|---|---|---|---|
| BLS-only | 25 | 77 | 4.75% | 94 |
| 70/30 (live) | 25 | 55 | 3.28% | 106 |
| 60/40 | 25 | 51 | 3.06% | 113 |
| 50/50 | 25 | 49 | 2.92% | 161 |
| ZORI-only | 25 | 81 | 4.90% | 400 |

### Per-region MAPE vs ground truth = BLS at T+12 (live 70/30 weight)

| Region | N anchors | MAPE | MAE ($) |
|---|---|---|---|
| State | 5 | 3.65% | 66 |
| Honolulu | 5 | 3.82% | 75 |
| Hawaii | 5 | 2.89% | 36 |
| Maui | 5 | 2.42% | 40 |
| Kauai | 5 | 3.64% | 56 |

## Recommendation

- Under **blend ground truth**, lowest-MAPE scheme is **ZORI-only** (3.03%); live 70/30 sits at 5.75%.
- Under **BLS-only ground truth**, lowest-MAPE scheme is **50/50** (2.92%); live 70/30 sits at 3.28%.

These two ground-truth constructions bracket the true accuracy of the live nowcast. The blend-truth view favors lower CPI weights (it is correlated with ZORI by construction); the BLS-only-truth view favors higher CPI weights. The live 70/30 lives near the midpoint and is reasonably defensible under both views.

The live `BLENDED_RENT_CPI_WEIGHT = 0.70` is **not** auto-modified by this harness. If a future review wants to retune, the per-region tables above are the most granular signal (Honolulu has the cleanest ACS + full ZORI history; Kauai falls back on a state ZORI ratio for some anchors).

## Caveats
- ACS B25058_001E is *contract* rent (utilities excluded), comparable to ZORI but not directly to BLS rent of primary residence. The blend is internally consistent because all components apply growth ratios to a single ACS dollar value.
- Kauai ZORI history started recently; vintage-year averages may use a state-fallback ratio when the county itself is missing.
- 5 anchors × 5 regions = 25 max cells per ground-truth view. Sample is small; treat MAPE differences between schemes as directional, not statistically definitive.
- The harness does **not** itself project T → T+12 into the future with the blend; it tests how stable the blend's nowcast is over a 12-month horizon when measured against the proxies above.

