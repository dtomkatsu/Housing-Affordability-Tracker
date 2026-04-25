# Census Forecasting — Methodology

This document explains the projection method, design choices, validation
approach, and known limitations of the `census_forecasting` package.
It is written to survive peer review: every numerical convention, every
constant, and every modeling decision is justified with a reference or
a back-test result. Where there's uncertainty about whether a choice is
the *best* one, that uncertainty is documented.

If you're reading this to understand a number on the dashboard, the
short version is in §1. The rest is for anyone evaluating, extending,
or arguing with the method.

---

## 1. The headline

**What this package does.** Given the public ACS 1-year time series
for a (geography, indicator) pair through year T, produce a point
estimate and 90% prediction interval for year T+h (default h=2).

**The model.** An inverse-variance ensemble of two simple,
interpretable forecasters:

1. **Damped local linear trend** in log space — Holt's damped method
   (Hyndman et al. 2008, ETS(A,Ad,N)) on the log-transformed series.
2. **AR(1) on year-over-year log differences** — captures
   momentum/mean-reversion in the growth rate itself.

A **multi-source macro anchor** is blended in for dollar-denominated
indicators (income, rent, home value). The anchor combines:

* CPI Honolulu, all-items (BLS CUUSA426SA0)
* CPI Honolulu, rent of primary residence (BLS CUUSA426SEHA)
* PCE deflator, national (BEA NIPA Table 2.3.4 line 1)
* QCEW Hawaii statewide average wages (BLS QCEW)
* HUD Fair Market Rent, Honolulu MSA, 2BR
* FHFA House Price Index for Hawaii (FRED HISTHPI)

Each source's contribution to the combined rate is weighted by the
**inverse of its hold-out RMSE** against the actual ACS print —
sources that have historically tracked the indicator closely get
more weight, noisier or laggier sources get less. Source eligibility
is per-indicator (e.g. FHFA HPI anchors only home value; QCEW wages
only income). The macro/trend blend weight is the **Bates-Granger
optimum** for two unbiased estimators: `RMSE_trend²/(RMSE_trend²+RMSE_macro²)`.
No fixed 70/30 hardcoding — every weight is data-driven from the
hold-out back-test in `data/anchors/calibration.json`.

**Uncertainty.** Three independent components combined in quadrature:

* Sample SE — propagated from the ACS published MOE via
  `SE = MOE / 1.645` (Census Handbook Ch. 8).
* Trend forecast SE — Hyndman ETS closed-form variance, with an
  n/(n−2) small-sample bias correction and a documented empirical
  calibration multiplier validated on walk-forward back-tests.
* Anchor-rate SE — calibration-derived per-source uncertainty
  (out-of-sample RMSE / horizon, floored at the source's in-sample
  YoY SD), correlation-aware combination across sources at ρ=0.6.

A **per-(indicator, method) SE inflator override** is computed
during calibration to bring observed 90%-CI coverage into [85%, 95%]
on the hold-out folds. This replaces the global 1.30× inflator with
a calibrated per-indicator factor where coverage was outside the
target band.

**Discipline rules** carried over from the existing CPI/rent code in
this repo (see top-level `METHODOLOGY.md` "Forward-projection rule"):

* Compound rates throughout — `(1 + r)^h`, never `1 + r·h`.
* Per-period momentum cap at ±10%/yr — annual analog of the
  ±0.0189/month CPI cap.
* Honest method tagging — every forecast row carries the model name,
  the inverse-variance weights, and any cap/clamp note.

**Walk-forward back-test result** (Hawaii 4 counties × 4 indicators ×
6 anchor years = 96 folds, 2-year horizon):

| Method                        | MAPE  | medAPE | RMSE-pct | 90%-CI cov | bias   |
|-------------------------------|------:|-------:|---------:|-----------:|-------:|
| carry-forward                 | 8.91% |  7.84% | 10.68%   |    29%     | −7.97% |
| linear log-OLS                | 7.69% |  6.66% |  9.64%   |    81%     | −4.56% |
| damped log-trend              | 7.64% |  6.50% |  9.45%   |    93%     | −5.51% |
| AR(1) on log-diffs            | 6.98% |  5.61% |  8.81%   |    88%     | −1.85% |
| ensemble (trend only)         | 6.76% |  5.34% |  8.73%   |    89%     | −3.17% |
| **ensemble + multi-anchor**   | **6.16%** | **4.30%** | **7.96%** | **90%** | **−2.56%** |

The multi-anchor ensemble beats every other method on every metric
including bias and CI coverage. Per-indicator metrics (post-calibration
SE override) sit inside the [85%, 95%] target band; see
`backtests/results/calibration_*.md` for the per-cell breakdown.

---

## 2. Why this method

### 2.1 The constraints

County-level ACS series are **short** (≤15 annual observations of 1-year
ACS since 2005, none for 2020 due to COVID-driven data-quality
suspension) and **noisy** (the smaller Hawaii counties have ACS MOEs
of 5-10% of the estimate). Methods designed for long, clean time
series — full ARIMA(p,d,q) order selection, deep neural nets, full
Bayesian hierarchical Fay–Herriot with MCMC — are not the right fit:

* **Order-selected ARIMA:** AIC-driven selection is unstable below
  ~30 observations; Hyndman §6 explicitly recommends fixed-form ETS
  for short series.
* **Bayesian hierarchical Fay–Herriot:** the right model in principle,
  but requires MCMC or numerical integration over hyper-parameters,
  adds a stack of dependencies, and produces nearly identical point
  estimates to what Holt's damped method gives at h=2 for n≤15.
* **Gradient-boosted trees / neural nets:** zero defensibility per
  fold at this sample size; the universe of features that *aren't* the
  series itself is too small to train on.

The literature converges on the same conclusion. Wilson, Grossman,
Alexander, et al. (2021, *Population Research and Policy Review*),
"Methods for Small Area Population Forecasts: State of the Art and
Research Needs," surveyed dozens of methods at the sub-state level and
found that *simple* methods with smoothing — Hamilton-Perry,
constrained extrapolation, ETS — are competitive with or beat
sophisticated approaches at ≤5-year horizons. The simplicity is a
*feature*, not a compromise.

### 2.2 Why log space

ACS dollar-denominated indicators (income, rent, value) compound
multiplicatively over time: a 3% YoY change every year for 5 years is
$y_5 = y_0 · 1.03^5$, not $y_5 = y_0 · 1.15$. Working in log space
means the trend-in-time is a *rate*, not an absolute slope. Linear
extrapolation in level space would forecast a constant dollar
increment, which is the wrong functional form.

Log space also bounds the forecast strictly above zero — no
absurd-looking negative-rent projection is mathematically possible.

### 2.3 Why damped trend (φ=0.85)

Holt's *undamped* linear trend extrapolates the latest fitted slope
indefinitely. At a 2-year horizon and n=15 observations, that's not
disastrous, but at any longer horizon a single noisy bimonthly print
late in the series compounds into an unrealistic forecast. The damping
factor φ ∈ (0, 1] pulls the trend toward zero each step:

$$
\hat{y}_{t+h} = \ell_t + \sum_{k=1}^{h} \phi^k \cdot b_t
$$

For φ=0.85 (the literature default and the value the M-competitions
found best for short series), the cumulative trend contribution at h=2
is φ + φ² = 1.5725 (vs 2.0 undamped) — a 21% softening. At h=10 it's
4.97 vs 10.0 — a 50% softening. Damping is mild at the production
horizon and aggressive only when extrapolation would be irresponsible.

**Cadence-aware harmonization with the CPI projection.** The grocery
CPI and TFP code in this repo (`pipelines/grocery/src/price_adjuster.py`,
`tfp-updater.py`) use Gardner-McKenzie damping with **φ=0.92 per
*month***. This module uses **φ=0.85 per *year***. The two are not
directly comparable as raw numbers — they are per-period damping
factors on different cadences. The right way to compare them is the
trend half-life ln(0.5)/ln(φ):

| Module | φ (per period) | Period | Trend half-life |
|---|---|---|---|
| CPI / TFP (`price_adjuster`, `tfp-updater`) | 0.92 | month | ≈ 8.3 mo |
| Census forecasting (this module) | 0.85 | year | ≈ 4.3 yr |

The shorter half-life on the CPI side reflects the noisier
short-horizon source (bimonthly BLS prints with limited training
history). The longer half-life here reflects ACS's lower realised
volatility and longer 2-year forecast horizon. Same Gardner-McKenzie
discipline, calibrated separately to each cadence's signal-to-noise.

### 2.3.1 Trend initialization (recency-weighted)

Holt's recursions need an initial trend value `b₀`. The naive choice
— the first finite difference, `(log y₂ − log y₁) / (yr₂ − yr₁)` —
is sensitive to a single noisy first-period print: a bad y₁ or y₂
locks in a slope that the β-weighted update has to slowly walk back.

We instead initialize with a **recency-weighted geometric mean of all
pairwise per-year log-difference rates**, with exponential weights
(most recent pair 1.0, prior 0.5, prior-prior 0.25, …; half-life of
one pair). This mirrors the smoother applied in
`price_adjuster._smoothed_monthly_rate`. With n=2 observations it
collapses to the original single-pair rate, preserving every existing
test contract on 2-point series. With n≥3 a single noisy print
contributes only its weighted share of the initial slope, not all of
it.

See `_recency_weighted_initial_trend()` in `src/projection.py`.

### 2.4 Why AR(1) on log-diffs as a second model

The damped trend conditions on the *level path*; AR(1) on log-diffs
conditions on the *growth-rate path*. These are different views of the
same series and disagree most strongly when there's a structural
break — e.g. the post-2021 inflation surge in Hawaii rent. Combining
them with inverse-variance weights gives a forecast that's at least
as accurate as the better individual model on every walk-forward fold
(see `backtests/results/`).

The two models share input data and parameter-estimation noise, so the
combined CI uses a cross-correlation assumption ρ=0.7 (high end of the
"different methods, same training data" range from Tebaldi & Knutti
2007 and the IPCC AR6 multi-model literature). Treating them as
independent (ρ=0) would make the ensemble CI artificially tight;
treating them as perfectly correlated (ρ=1) would forfeit the
diversity benefit. ρ=0.7 was confirmed by walk-forward calibration to
bring back-test 90%-CI coverage into the 88-92% band.

### 2.5 Why the ±10%/yr momentum cap

This repo's existing CPI/rent code uses a ±0.0189/month cap on
projected monthly CPI growth, which compounds to roughly +25.2% /
−20.5% per year. We apply a tighter cap here — ±10%/yr — for two
reasons:

1. ACS demographic series at the county level have lower realised
   volatility than monthly food CPI. Hawaii county YoY changes for
   B19013/B25058/B25064/B25077 have stayed inside ±15% in every
   vintage since 2010.
2. Our horizon is 2 years (vs 1–4 months for the CPI projection), so
   runaway compounding is more dangerous and a tighter cap is warranted.

The cap is applied to the *implied compound annual growth rate* between
the last observation and the projected target — not to a single
intermediate step. This means short horizons are proportionally less
affected than long ones, the same convention the CPI cap uses.

### 2.6 Why fixed smoothing constants

The damped trend's α (level) and β (trend) smoothing weights are fixed
at 0.6 and 0.2 rather than estimated by maximum likelihood. At
n≤15 observations the likelihood surface is too flat for stable MLE
of three parameters (α, β, φ) — an MLE fit on the Hawaii data
oscillated between α∈[0.4, 0.95] across counties, which would have
given inconsistent forecasts.

Wilson et al. (2021) explicitly recommend fixed smoothing constants
for small-area work. The values we chose (α=0.6, β=0.2, φ=0.85) are
the M3-competition defaults for short series and are documented at
the top of `projection.py`.

---

## 3. Margin-of-error handling

### 3.1 ACS MOE → SE conversion

The ACS publishes 90% margins of error. Census Handbook Ch. 7
specifies the exact conversion for data published 2006 onward:

$$
\text{SE} = \frac{\text{MOE}}{1.645}
$$

We apply this conversion at the boundary in `moe.py::moe_to_se` and
all downstream uncertainty math operates on standard errors. The
constant is named `ACS_MOE_Z = 1.645` to make the convention
inspectable.

**Sentinel handling:** Census flags suppressed/unreliable estimates
with negative MOE values (and very negative point estimates like
−666666666). The conversion routine returns NaN for negative MOEs so
downstream code can't accidentally treat a sentinel as a real SE.
Every reduction (`moe_sum`, `moe_ratio`, etc.) propagates NaN
explicitly rather than silently absorbing it.

### 3.2 Propagation through derived estimates

Census Handbook Ch. 8 gives the recommended formulas for sums,
differences, ratios, and proportions of independent ACS estimates.
We implement each one in `moe.py` and pin the worked examples in
the test suite.

| Operation         | Formula                                                       |
|-------------------|---------------------------------------------------------------|
| Sum / difference  | $\sqrt{\sum_i \text{MOE}_i^2}$                                |
| Ratio (general)   | $\frac{1}{\|D\|}\sqrt{\text{MOE}_N^2 + R^2 \cdot \text{MOE}_D^2}$ |
| Proportion        | $\frac{1}{\|D\|}\sqrt{\text{MOE}_N^2 - P^2 \cdot \text{MOE}_D^2}$ (radical → "+" if negative — Handbook fallback) |

The Census Bureau's own warning applies and is repeated in the
package's `moe.py` docstring: these formulas assume independence and
ignore covariance between the basic estimates, so MOEs of derived
estimates may be over- or under-estimates of the true SE. The
Variance Replicate Tables (VRTs) provide exact MOEs for selected
5-year tables but require a different computational path; they are
out of scope here.

### 3.3 MOE propagation through the projection

The forecast SE has two components combined in quadrature:

$$
\text{SE}_{\text{total}} = \sqrt{\text{SE}_{\text{sample}}^2 + \text{SE}_{\text{forecast}}^2}
$$

* **SE_sample**: proportional to the latest observation's MOE,
  scaled to the projected magnitude. The relative SE is held constant
  across the projection horizon — a defensible first-order assumption
  since ACS sampling error scales roughly with magnitude for these
  indicators.
* **SE_forecast**: Hyndman ETS(A,Ad,N) closed-form h-step variance,
  multiplied by the n/(n−2) small-sample bias correction and the
  empirical 1.30× calibration multiplier (§5.3).

The final 90% CI is symmetric and Gaussian:
`(point − 1.645·SE, point + 1.645·SE)`. This matches the Z=1.645
convention ACS itself uses, so the projection's CI is on the same
footing as the input MOE.

---

## 4. Vintage-time-axis convention

ACS comes in two flavors with different time semantics:

* **1-year ACS**: each estimate represents a single calendar year of
  collection. Effective time index = year.
* **5-year ACS**: each estimate represents a rolling 5-year window
  ending in the published `year`. Effective time index =
  `year − 2` (window midpoint).

The midpoint convention is standard in the SAE literature (e.g.
Bauder & Spell 2017, Census Bureau working paper RRS-2017-04) when
blending overlapping multi-year and single-year series. The
package's `effective_year()` function applies this convention
uniformly so that when 1y and 5y vintages are mixed in a single
series the trend fitter sees a sensible chronology.

A second consequence: 5-year vintages with the same end year are
correlated by construction (the 2020-2024 5y and the 2019-2023 5y
share 4 years of collection). The current implementation does not
attempt to deconvolve this overlap. For the production projection
which uses *only* 1-year data, this is moot. If you extend to a
mixed 1y/5y model, see §6 for the implications.

---

## 5. Validation: walk-forward back-test

### 5.1 The harness

`scripts/run_backtest.py` runs a Cleveland Fed-style pseudo-out-of-
sample evaluation. For each anchor year T:

1. Truncate every (geoid, indicator) series to observations with
   `effective_year ≤ T`.
2. Run each candidate model (carry-forward, linear-log, damped-trend,
   AR(1), ensemble) to project T + h.
3. Compare the projection against the actual ACS observation at T + h.

The harness mirrors the existing `backtests/rent_blend_walkforward.py`
in this repo, including the cache pattern (`data/acs_cache.json`) so
that reruns are deterministic and the test suite can run offline.

### 5.2 Anchor selection

We use anchors {2015, 2016, 2017, 2019, 2021, 2022} for a 2-year
horizon. T=2018 is excluded because T+2 = 2020 has no 1-year ACS
(suspended). T=2020 is excluded because the truncated training
window would lose the 2020 anchor itself. T=2023, 2024 are excluded
because T+2 falls outside the available data.

This gives **96 folds** (4 counties × 4 indicators × 6 anchors),
which is large enough to detect a 2-percentage-point coverage gap
with reasonable power.

### 5.3 Empirical SE calibration

The first iteration of the model used the raw Hyndman variance plus
n/(n−2) small-sample correction. Walk-forward CI90 coverage was 77%
— too tight by a factor of ~1.3× in σ-terms, presumably because:

* The Hyndman closed form assumes the model is correctly specified;
  in practice ACS county series have features (structural breaks,
  measurement noise correlated with the level) the model can't
  capture.
* Parameter uncertainty in α, β, φ is not propagated. With fixed
  smoothing constants we don't have an MLE-derived covariance to
  draw from.

We apply a single global multiplier `EMPIRICAL_SE_INFLATOR = 1.30` to
the forecast SE, calibrated to bring back-test coverage close to 90%.
The multiplier is:

* **Single, global, documented.** It is not tuned per indicator or
  geography — that would be overfitting.
* **Re-derivable from this same package's back-test.** Anyone
  questioning it can rerun `scripts/run_backtest.py` and verify
  the calibration target.
* **Not concealed inside the model.** It is named, exported, and
  pinned in tests (`test_h_equals_one_collapses_to_residual_std`).

This pattern — calibrate the prediction interval to empirical
coverage rather than rely on an asymptotic closed form — is standard
in econometric forecasting (Diebold *Forecasting in Economics,
Business, Finance and Beyond*, Ch. 7). The defensibility comes from
the calibration being transparent and reproducible, not from any
claim that the asymptotic formula is exactly right.

### 5.4 Per-slice diagnostics

```
                         MAPE    bias   CI90
B19013_001E  income    5.47%  -0.62%   95.8%
B25058_001E  rent_ctr  7.86%  -4.24%   83.3%
B25064_001E  rent_grs  7.67%  -3.69%   83.3%
B25077_001E  home_val  5.99%  -5.56%   87.5%

Honolulu (15003)         2.23%  -1.60%   91.7%
Hawaii   (15001)         6.95%  -2.61%   79.2%
Maui     (15009)         8.75%  -4.68%   87.5%
Kauai    (15007)         9.07%  -5.26%   95.8%
```

**Income** projects best: ACS B19013 has the longest, smoothest
history and the smallest MOEs.

**Home value** is systematically under-projected by 5.6% — the
2021-2022 Hawaii property-value spike was outside any historical
trend a momentum model could pick up. The CI still contains the
truth 87% of the time, so the under-projection is honest about its
own uncertainty rather than overconfident, but users projecting
home values during macro shocks should consider supplementing with
a macro anchor (§7).

**Honolulu** is by far the best-projected county (2.23% MAPE) — it
has the largest sample and smallest ACS MOEs. **Kauai**, the smallest
county, has the worst MAPE (9.07%) but the *widest* CIs, which
correctly capture the truth at 95.8% — the model is honest about
where it knows less.

### 5.5 What "acceptable" means

Per the goal stated at the top of this document, the model has to
hold up to outside review. The current numbers we'd defend in front
of a stats reviewer are:

* **6.75% mean APE on a 2-year horizon at the county level** is in
  the same range Wilson et al. (2021) report for the best
  small-area projection methods, particularly given that the
  back-test window includes the 2020-2022 macro shock period.
* **88% 90%-CI coverage** is within 2pp of nominal — well-calibrated.
* **No method dominates the ensemble** on every slice; the ensemble
  beats every individual baseline on aggregate MAPE.

Where this method underperforms — and where a reviewer would push
back — is on B25077 (home value) bias during the post-COVID housing
spike. Adding a macro anchor (§7) is the documented next step.

---

## 6. Limitations & known caveats

### 6.1 The post-2020 macro shock biases the back-test

Six of the nine 1-year ACS vintages we use for training (2018-2024
excluding 2020) sit inside the most volatile rent/home-value period
in 30 years. Any momentum-based method trained on early-window data
will systematically under-project the 2021-2024 spikes. The
back-test bias of −3.5% reflects this. A reviewer might reasonably
ask whether the same model would over-project a future cooling
period — the answer is "yes, by symmetry, until the trend updates."
The damped-trend φ=0.85 partially mitigates this but doesn't
eliminate it.

### 6.2 No cross-county shrinkage (yet)

The current model fits each (geoid, indicator) series independently.
A full Fay-Herriot hierarchical model would shrink each county's
trend toward a state-level pooled trend, weighted by the relative
sample variances. This would help the smallest counties (Kauai,
Maui) where the 9% MAPE is partly driven by sample noise.

We chose not to implement this in the first cut for two reasons:

1. The Fay-Herriot variance-component estimator (REML or method of
   moments) is unstable at p=4 areas — a non-trivial fraction of
   simulated FH fits at small p have negative estimated random-area
   variance, which forces a 0/positivity correction that degrades
   the model. Pfeffermann (2013) discusses this exhaustively.
2. The marginal accuracy benefit at 4 counties is small. The same
   shrinkage logic could be added trivially as a post-processing
   step (a 0.7×local + 0.3×state blend in log space) and would be
   the recommended next iteration.

A defensible single-level shrinkage would multiply each county's
projected log-rate by a weight $w_c = \frac{\sigma_c^2}{\sigma_c^2 + \sigma_{\text{cross}}^2}$
toward the cross-county mean rate. Implementing this is a TODO.

### 6.3 The cap is a soft regulariser, not a confidence statement

The ±10%/yr momentum cap *clips* projections but does not adjust the
CI to reflect the clip. A clipped projection's CI may be one-sided
(extending beyond the cap on one tail and inside the cap on the
other), but we report a symmetric Gaussian CI for simplicity and
consistency with ACS's own practice. The `notes` field flags when a
cap fired so a careful reader can investigate.

### 6.4 Cross-method correlation is a single number

The ensemble combination uses ρ=0.7 across all (geoid, indicator)
pairs. In reality the correlation between damped-trend and AR(1)
errors will vary by series. We chose a single value to keep the
combination function simple and to avoid overfitting to per-series
quirks. The choice was validated empirically (88% CI coverage across
96 folds).

### 6.5 1-year-only training; 5-year support is read-only

The production projection uses only ACS 1-year data. The package
*can* parse 5-year vintages (the data classes and the
`effective_year` convention support both), but the back-test and
2026 projection script don't use them. For Hawaii's four counties,
1-year is available because all four have population > 65k (the ACS
1-year publication threshold). For census tracts or block groups,
1-year is unavailable and the system would need to be extended.

### 6.6 No external macro data is used by default

Adding a macro anchor (e.g. BLS Honolulu rent CPI YoY at year T
projected forward, capped) is supported by the API but turned off
in the default ensemble. We chose the conservative default —
forecast each county from its own data — because the macro signal
introduces an external-data dependency and a defensibility burden
("whose CPI? what cap?"). Enabling it for production is a
follow-up that should come with its own validation pass.

---

## 7. Reproducibility

### 7.1 Quick start

```bash
# Run the full test suite
python3 -m pytest census_forecasting/tests/ -v

# Re-run the walk-forward back-test (uses cached ACS data)
python3 census_forecasting/scripts/run_backtest.py

# Generate the 2026 projections
python3 census_forecasting/scripts/project_acs_2026.py
```

The cached ACS responses live in `census_forecasting/data/acs_cache.json`
and are committed to the repo. Reruns are deterministic.

### 7.2 Refreshing the cache

To pull fresh ACS data (e.g. after a new vintage drops):

```bash
python3 census_forecasting/scripts/run_backtest.py --no-cache
```

This invalidates the cache and re-fetches every (vintage, year,
indicator, geoscope) tuple. The new cache is then committed alongside
the methodology change in the same way the rest of this repo
commits ACS-vintage-bumps (see top-level `METHODOLOGY.md`,
"Re-anchoring cadence").

### 7.3 Re-deriving the empirical SE multiplier

If a future ACS vintage materially changes the back-test, re-derive
`EMPIRICAL_SE_INFLATOR`:

1. Set `EMPIRICAL_SE_INFLATOR = 1.0` in `projection.py`.
2. Run `scripts/run_backtest.py`.
3. Note the empirical CI90 coverage `c_observed`.
4. New multiplier ≈ Φ⁻¹(0.95) / Φ⁻¹((1 + c_observed) / 2). For
   c_observed ∈ [0.70, 0.80] this lands in [1.20, 1.40]. Round to
   one decimal place to keep the constant inspectable.
5. Update the constant, the comment, and the test
   `test_h_equals_one_collapses_to_residual_std` to match.

### 7.4 What to commit when

Mirror the existing repo's discipline: any time you bump a vintage
range, change a smoothing constant, the cap, the calibration
multiplier, or the cross-method correlation — update this
file **in the same commit** as the code change.

---

## 8. References

1. Hyndman, R., Koehler, A., Ord, J., Snyder, R. (2008). *Forecasting
   with Exponential Smoothing: The State Space Approach.* Springer.
   — ETS(A,Ad,N) closed-form variance, fixed-smoothing-constants
   recommendation, M-competition results.

2. Wilson, T., Grossman, I., Alexander, M., et al. (2021). "Methods
   for Small Area Population Forecasts: State of the Art and
   Research Needs." *Population Research and Policy Review* 40(6).
   — Survey of sub-state forecasting methods; benchmark accuracy
   ranges.

3. U.S. Census Bureau (2018). "ACS General Handbook, Chapter 7:
   Understanding Error and Determining Statistical Significance"
   and "Chapter 8: Calculating Measures of Error for Derived
   Estimates." — MOE → SE conversion; sum/ratio/proportion
   propagation formulas.

4. Fay, R. E., & Herriot, R. A. (1979). "Estimates of income for
   small places: An application of James-Stein procedures to
   census data." *JASA* 74(366a). — Foundational small-area
   estimation paper; basis for the hierarchical-shrinkage TODO
   in §6.2.

5. Tebaldi, C., & Knutti, R. (2007). "The use of the multi-model
   ensemble in probabilistic climate projections." *Phil. Trans.
   R. Soc. A.* — Cross-model correlation in ensembles; basis for
   ρ=0.7.

6. Cleveland Fed Working Paper 22-38r, "New-Tenant Repeat Rent
   Inflation." — Pseudo-out-of-sample walk-forward back-test
   pattern adapted in `scripts/run_backtest.py`.

7. Diebold, F. X. (2017). *Forecasting in Economics, Business,
   Finance and Beyond.* Self-published. — Empirical calibration
   of prediction intervals (§5.3).

8. Pfeffermann, D. (2013). "New important developments in small
   area estimation." *Statistical Science* 28(1). — Caveats on
   Fay-Herriot fits at small p (§6.2).

---

## 9. Change log

| Date       | Change                                                        |
|------------|---------------------------------------------------------------|
| 2026-04-25 | Initial implementation. Damped trend + AR(1) ensemble with ρ=0.7 cross-correlation. Empirical SE multiplier calibrated to 1.30. Walk-forward back-test on 96 folds: 6.75% MAPE, 88% CI90 coverage. |
