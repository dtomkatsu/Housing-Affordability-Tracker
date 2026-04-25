# Anchor calibration
**Run date:** 2026-04-25
**Anchor years:** [2015, 2016, 2017, 2019, 2021, 2022]
**Horizon:** 2y
**Coverage band:** [85%, 95%]

## Per-(indicator, source) RMSE

Lower RMSE → higher weight in the multi-source anchor combiner.

### B19013_001E

| Source | RMSE (pct error) |
|---|---:|
| qcew_hawaii_wages | 6.53% |
| pce_deflator | 6.98% |
| cpi_honolulu_allitems | 7.08% |

### B25058_001E

| Source | RMSE (pct error) |
|---|---:|
| hud_fmr_honolulu | 9.52% |
| cpi_honolulu_rent | 9.89% |

### B25064_001E

| Source | RMSE (pct error) |
|---|---:|
| hud_fmr_honolulu | 8.87% |
| cpi_honolulu_rent | 9.00% |

### B25077_001E

| Source | RMSE (pct error) |
|---|---:|
| fred_hi_hpi | 6.16% |

## Per-(indicator, method) RMSE + CI90 coverage

### B19013_001E

| Method | RMSE | CI90 coverage |
|---|---:|---:|
| multi_anchor | 6.77% | 95.8% |
| trend_ensemble | 7.03% | 95.8% |

### B25058_001E

| Method | RMSE | CI90 coverage |
|---|---:|---:|
| multi_anchor | 9.45% | 87.5% |
| trend_ensemble | 10.37% | 87.5% |

### B25064_001E

| Method | RMSE | CI90 coverage |
|---|---:|---:|
| multi_anchor | 8.66% | 95.8% |
| trend_ensemble | 9.72% | 83.3% |

### B25077_001E

| Method | RMSE | CI90 coverage |
|---|---:|---:|
| multi_anchor | 6.16% | 95.8% |
| trend_ensemble | 7.32% | 87.5% |

## SE inflator overrides (where coverage outside [85%, 95%])

| Indicator | Method | Override factor |
|---|---|---:|
| B19013_001E | multi_anchor | 1.050 |
| B19013_001E | trend_ensemble | 1.175 |
| B25064_001E | multi_anchor | 1.050 |
| B25064_001E | trend_ensemble | 1.839 |
| B25077_001E | multi_anchor | 1.050 |

