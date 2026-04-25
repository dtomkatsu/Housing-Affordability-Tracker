"""Anchor source loaders and registry.

A "source" wraps a JSON file under `data/anchors/` and exposes:

* `load_series(end_year)` → ordered list of (year, value) limited to
  observations whose *publication year* is ≤ end_year (so back-tests
  using a hidden-data anchor year T can never accidentally peek at
  values that hadn't been published yet).
* `annual_log_rates(end_year)` → list of (year, log_rate, weight) where
  log_rate is the YoY log-difference and weight is the recency-weighted
  smoother weight used in `projection._recency_weighted_initial_trend`.
* `smoothed_annual_rate(end_year)` → the recency-weighted geometric
  mean of YoY log rates as the forward-anchor for compound projection.

The "publication year" handling is the key difference from a naive
"slice the dict at year T" — see `publication_lag_years` per source.
For example HUD FMR FY2024 was *published* in mid-2023 based on 2021
ACS data; if we're back-testing as of anchor year 2021, we cannot use
the FY2024 FMR (it didn't exist yet). The lag accounts for this.

The loaders carry no internal state and are safe to call concurrently;
the JSON files themselves are read once per call (small, <2KB each).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_ANCHOR_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "anchors"


@dataclass(frozen=True)
class AnnualRate:
    """A single year's YoY log-rate observation from a source.

    `log_rate` is annualised log-difference: ln(value_t / value_{t-gap}) / gap.
    For an annual series gap=1; for irregular gaps (e.g. semiannual data
    averaged to year-of-publication) we still annualise so all rates are
    comparable.
    """
    year: int
    log_rate: float
    se_log_rate: float


@dataclass(frozen=True)
class AnchorSource:
    """Wrapper around an embedded historical series.

    Attributes
    ----------
    name : str           — short stable identifier (e.g. "cpi_honolulu_allitems")
    data : dict          — parsed JSON (loaded eagerly at construction)
    publication_lag_years : int
        How many years a value lags reality. Most series here lag 0
        (we list calendar-year values published the following year);
        HUD FMR is special because its *fiscal* year is offset (FY2024
        FMR comes out mid-2023 based on 2021 ACS).
    indicator_affinity : tuple of ACS table-cell IDs the source can
        anchor. Used to filter sources when selecting macro anchors
        for a given ACS indicator.
    rate_se_floor : float
        Per-source minimum standard error on YoY log-rate (to prevent
        an unreasonably tight ensemble weight from a single low-noise
        anchor with a few atypical years). Documented in METHODOLOGY.md.
    """
    name: str
    path: Path
    publication_lag_years: int
    indicator_affinity: tuple[str, ...]
    rate_se_floor: float
    _data: dict

    @classmethod
    def from_file(
        cls,
        path: Path,
        publication_lag_years: int,
        indicator_affinity: tuple[str, ...],
        rate_se_floor: float = 0.005,
    ) -> "AnchorSource":
        with open(path) as f:
            data = json.load(f)
        return cls(
            name=path.stem,
            path=path,
            publication_lag_years=publication_lag_years,
            indicator_affinity=indicator_affinity,
            rate_se_floor=rate_se_floor,
            _data=data,
        )

    @property
    def metadata(self) -> dict:
        return {
            "name": self.name,
            "source": self._data.get("source"),
            "series_id": self._data.get("series_id"),
            "title": self._data.get("title"),
            "frequency": self._data.get("frequency"),
            "units": self._data.get("units"),
            "limitations": self._data.get("limitations", []),
            "publication_lag_years": self.publication_lag_years,
            "indicator_affinity": list(self.indicator_affinity),
        }

    def load_series(self, end_year: Optional[int] = None) -> list[tuple[int, float]]:
        """Return [(year, value), ...] sorted ascending, limited by visibility.

        With end_year=T, a value for year Y is included only if it would
        have been *published* on or before T — i.e. Y + publication_lag_years
        ≤ T. This is the no-peeking rule for hidden-data back-tests.
        """
        items: list[tuple[int, float]] = []
        for k, v in self._data.get("values_by_year", {}).items():
            try:
                yr = int(k)
            except ValueError:
                continue
            if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
                continue
            if v <= 0:
                continue
            if end_year is not None and (yr + self.publication_lag_years) > end_year:
                continue
            items.append((yr, float(v)))
        items.sort(key=lambda x: x[0])
        return items

    def annual_log_rates(self, end_year: Optional[int] = None) -> list[AnnualRate]:
        """YoY log-rates with empirical residual SE.

        SE is the dispersion of pairwise log-rates within the visible
        window. Floored at `rate_se_floor` so a perfectly smooth admin
        series doesn't get unreasonably high weight in the ensemble.
        """
        series = self.load_series(end_year)
        if len(series) < 2:
            return []
        rates: list[tuple[int, float]] = []
        for i in range(1, len(series)):
            (y0, v0), (y1, v1) = series[i - 1], series[i]
            gap = y1 - y0
            if gap <= 0 or v0 <= 0 or v1 <= 0:
                continue
            rates.append((y1, math.log(v1 / v0) / gap))
        if not rates:
            return []
        # Empirical SD of YoY log-rates as the residual SE; floor it.
        rs = [r for _, r in rates]
        mean_r = sum(rs) / len(rs)
        if len(rs) >= 2:
            var = sum((r - mean_r) ** 2 for r in rs) / (len(rs) - 1)
            sd = math.sqrt(var)
        else:
            sd = abs(rs[0])
        sd = max(sd, self.rate_se_floor)
        return [AnnualRate(year=y, log_rate=r, se_log_rate=sd) for y, r in rates]

    def smoothed_annual_rate(
        self, end_year: Optional[int] = None
    ) -> Optional[AnnualRate]:
        """Recency-weighted geometric mean of YoY log-rates.

        Mirrors `projection._recency_weighted_initial_trend` so the
        smoother is applied identically to both ACS-internal and external
        anchor series. Most-recent rate weight 1.0, prior 0.5, prior^2
        0.25, half-life one year (per pair).

        SE on the smoothed rate accounts for the fact that the weighted
        mean is more stable than a single pair: var = Σ w_i² σ² / (Σ w_i)²
        with σ ≈ residual SD of the YoY series.
        """
        rates = self.annual_log_rates(end_year)
        if not rates:
            return None
        n = len(rates)
        if n == 1:
            return rates[0]
        weights = [0.5 ** (n - 1 - i) for i in range(n)]
        wsum = sum(weights)
        smoothed = sum(rates[i].log_rate * weights[i] for i in range(n)) / wsum
        sigma = rates[-1].se_log_rate  # all rates share the same SD by construction
        # Variance of weighted mean of (assumed) iid draws.
        var_smoothed = sum(w * w for w in weights) * sigma * sigma / (wsum * wsum)
        # Floor: cannot beat a single observation's SE.
        se_smoothed = max(math.sqrt(var_smoothed), self.rate_se_floor)
        return AnnualRate(year=rates[-1].year, log_rate=smoothed, se_log_rate=se_smoothed)


# -----------------------------------------------------------------------------
# Source registry — which anchors are appropriate for which ACS indicators
# -----------------------------------------------------------------------------
#
# `indicator_affinity` is the set of ACS table-cell IDs each source is
# admissible to anchor. Conservative by construction: rent CPI anchors
# only the rent indicators; FHFA HPI anchors only home-value; QCEW wages
# and PCE/CPI broad inflation can anchor income.

_REGISTRY_SPEC = [
    # (filename, publication_lag_years, indicator_affinity, rate_se_floor)
    # CPI Honolulu broad inflation: anchors income (used as nominal
    # purchasing-power-equivalent macro rate). publication_lag=0:
    # H1+H2 averages are out by Dec of the year.
    ("cpi_honolulu_allitems.json", 0,
     ("B19013_001E",), 0.005),
    # CPI Honolulu rent — anchors gross/contract rent.
    ("cpi_honolulu_rent.json", 0,
     ("B25058_001E", "B25064_001E"), 0.005),
    # PCE deflator (national) — anchors income as alternate macro proxy.
    ("pce_deflator.json", 0,
     ("B19013_001E",), 0.005),
    # QCEW HI wages — anchors income.
    ("qcew_hawaii_wages.json", 1,  # final annual averages release ~Aug of year+1
     ("B19013_001E",), 0.005),
    # HUD FMR Honolulu — *validation* anchor for rent (lags 2y so its
    # back-test weight will be small; primarily used for checks).
    ("hud_fmr_honolulu.json", 2,
     ("B25058_001E", "B25064_001E"), 0.010),
    # FHFA HPI Hawaii — anchors home-value.
    ("fred_hi_hpi.json", 0,  # Q4 release within Q1 of year+1
     ("B25077_001E",), 0.005),
]


def load_source(name_or_filename: str) -> AnchorSource:
    """Load one anchor source by name (filename stem or filename)."""
    if not name_or_filename.endswith(".json"):
        name_or_filename = name_or_filename + ".json"
    path = _ANCHOR_DIR / name_or_filename
    spec = next(
        (s for s in _REGISTRY_SPEC if s[0] == name_or_filename),
        None,
    )
    if spec is None:
        raise KeyError(f"No anchor spec registered for {name_or_filename!r}")
    _, lag, affinity, floor = spec
    return AnchorSource.from_file(
        path=path,
        publication_lag_years=lag,
        indicator_affinity=affinity,
        rate_se_floor=floor,
    )


def available_sources(indicator: Optional[str] = None) -> list[AnchorSource]:
    """All registered sources (optionally filtered to those that can anchor `indicator`)."""
    sources: list[AnchorSource] = []
    for fname, lag, affinity, floor in _REGISTRY_SPEC:
        path = _ANCHOR_DIR / fname
        if not path.exists():
            continue
        try:
            src = AnchorSource.from_file(
                path=path,
                publication_lag_years=lag,
                indicator_affinity=affinity,
                rate_se_floor=floor,
            )
        except (OSError, json.JSONDecodeError):
            continue
        if indicator is None or indicator in src.indicator_affinity:
            sources.append(src)
    return sources


SOURCE_REGISTRY = _REGISTRY_SPEC  # exposed for documentation/test discovery
