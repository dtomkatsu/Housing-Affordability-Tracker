"""External macro-anchor data sources for ACS projection.

Each source loads an annual time series from `data/anchors/*.json` and
exposes a uniform interface to compute YoY log-rates and a smoothed
end-of-series annual rate suitable for use as a forecast anchor.

Why these particular series
---------------------------
ACS published estimates are noisy, especially at county level for
1-year vintages (n ≈ 1,000-3,000 sample interviews per Hawaii county
per year). A single year's print can move 5-10% off trend purely from
sampling. Anchoring projections to *external* series with much smaller
sampling error reduces idiosyncratic ACS noise without removing the
county-level signal.

The selected sources span four orthogonal dimensions:

1. **Survey vs administrative**: CPI / PCE deflator (survey-based price
   indices) vs HUD FMR (administrative) vs QCEW (administrative payroll
   tax filings) vs FHFA HPI (transaction-based).
2. **Geography**: Honolulu CPI is metro-specific; QCEW is statewide;
   PCE is national; FHFA HPI is statewide. Mixing these reveals when
   metro-level shocks are driving an indicator vs aggregate trend.
3. **Indicator linkage**: rent CPI ↔ ACS rent; QCEW wages ↔ ACS income;
   FHFA HPI ↔ ACS home value.
4. **Release lag**: PCE ~1mo, CPI semiannual ~6mo, QCEW ~6mo, FMR ~12mo.
   Using only stale anchors at projection time would understate
   uncertainty; the back-test uses the *vintage available at anchor
   year T* — never the future-revised series.

References
----------
* BLS CPI Honolulu: https://www.bls.gov/regions/west/news-release/consumerpriceindex_honolulu.htm
* BEA PCE deflator: https://apps.bea.gov/iTable/?reqid=19&step=2&isuri=1&categories=survey
* BLS QCEW: https://www.bls.gov/cew/
* HUD FMR: https://www.huduser.gov/portal/datasets/fmr.html
* FHFA HPI: https://fred.stlouisfed.org/series/HISTHPI
"""
from .base import (
    AnchorSource,
    AnnualRate,
    available_sources,
    load_source,
    SOURCE_REGISTRY,
)

__all__ = [
    "AnchorSource",
    "AnnualRate",
    "available_sources",
    "load_source",
    "SOURCE_REGISTRY",
]
