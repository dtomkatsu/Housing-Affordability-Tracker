"""
pumd_extractor.py
-----------------
Extract a "typical Honolulu household monthly food-at-home spending" figure
from BLS Consumer Expenditure Public Use Microdata (CE PUMD) interview-survey
files and project it to neighbor islands using the receipt-derived basket
gradient.

The output is a side-statistic (alongside the existing receipt-derived
`monthlyFamily4`) — it does NOT feed pricing.

PUMD background
~~~~~~~~~~~~~~~
- Files: BLS publishes per-year ZIPs at
  https://www.bls.gov/cex/pumd/data/comma/intrvw{YY}.zip
  Each ZIP contains FMLI{Y}{Q}.csv (one row per consumer-unit-quarter) and
  MTBI{Y}{Q}.csv (one row per (CU-quarter, UCC) expenditure cell).
- "Urban Honolulu, HI" is one of the ~23 named PSUs in PUMD; we filter on
  the FMLI `PSU` column.
- Per BLS errata for 2023+ : do NOT use the FDHOMEPQ/FDHOMECQ summary
  variables (they were dropped from PUMD output). Aggregate the raw FAH
  UCCs from MTBI directly. FAH UCCs are those whose hierarchical code
  starts with `19` (food-at-home root) excluding `1909*` (groceries on
  trips).
- Each FMLI row covers ONE quarter (3 months); divide by 3 to get a
  monthly figure. The CE-recommended weight is `FINLWT21`.

Neighbor-island projection
~~~~~~~~~~~~~~~~~~~~~~~~~~
PUMD only resolves to the Honolulu PSU. To produce a "typical household"
figure for Hawaii / Maui / Kauai counties (and a state aggregate), we use
the receipt-derived basket gradient already computed in the grocery
pipeline:

    pumd_estimate[county] = pumd_honolulu × (basket_total[county] / basket_total[honolulu])

This preserves PUMD as the *absolute-level anchor* (real measured Honolulu
spending) and the receipts as the *spatial gradient* (real measured price
gaps across counties). The state estimate is a population-weighted mean.
"""
from __future__ import annotations

from dataclasses import dataclass

# Honolulu PSU codes seen in CE PUMD across recent vintages. The data itself
# uses a coded form; we accept any of these as "Urban Honolulu, HI". If the
# code in your year's data does not match, add it here (PUMD ships a
# psumap.txt in the dictionary that lists the year's labels).
HONOLULU_PSU_CODES = {"S49A", "S49B", "S49C", "S49D"}

# Population weights for state-level rollup (DBEDT 2024 estimates).
# Mirrors POP weights used in grocery-price-updater.py.
COUNTY_POP_WEIGHTS = {
    "Honolulu": 1016000,
    "Maui":      167000,
    "Hawaii":    201000,
    "Kauai":      73000,
}


@dataclass
class FAHResult:
    """Population-weighted monthly FAH for one stratum (e.g. family-of-4)."""
    monthly_fah: float
    n_households: int
    ci_95: tuple[float, float]   # half-width-based 95% CI on the weighted mean
    family_size_label: str


# ---------------------------------------------------------------------------
# UCC filtering
# ---------------------------------------------------------------------------
def is_fah_ucc(ucc: str | int) -> bool:
    """True if a UCC is a food-at-home expenditure (excludes 'on trip').

    BLS hierarchy:
      19* = food at home root
      1909* = food at home, on trip — exclude
    """
    s = str(ucc).strip()
    if not s.startswith("19"):
        return False
    if s.startswith("1909"):
        return False
    return True


# ---------------------------------------------------------------------------
# Family-size classification
# ---------------------------------------------------------------------------
def family_size_bucket(family_size: int | float) -> str:
    """Map raw FAM_SIZE to one of the dashboard buckets."""
    n = int(family_size)
    if n <= 1:
        return "1"
    if n == 2:
        return "2"
    if n == 3:
        return "3"
    return "4+"


# ---------------------------------------------------------------------------
# Inflation adjustment
# ---------------------------------------------------------------------------
def inflate_to(value: float, from_year: int, to_year: int,
               food_cpi_annual: dict[int, float]) -> float:
    """Inflate a dollar value from from_year → to_year using annual food CPI.

    food_cpi_annual: {year: annual_avg_index}. Missing year → identity (no inflation).
    """
    src = food_cpi_annual.get(from_year)
    dst = food_cpi_annual.get(to_year)
    if not src or not dst:
        return value
    return value * (dst / src)


# ---------------------------------------------------------------------------
# Core extractor (operates on pandas DataFrames; testable with synthetic data)
# ---------------------------------------------------------------------------
def extract_honolulu_fah(
    fmli_df,                             # rows: one per CU-quarter
    mtbi_df,                             # rows: one per (CU-quarter, UCC)
    *,
    fmli_year: int,
    food_cpi_annual: dict[int, float] | None = None,
    target_year: int | None = None,
    psu_codes: set[str] | None = None,
) -> dict:
    """Compute weighted monthly FAH for Honolulu PSU households.

    Returns
    -------
    {
      "by_size": {
        "1":  FAHResult,
        "2":  FAHResult,
        "3":  FAHResult,
        "4+": FAHResult,
      },
      "overall": FAHResult,           # all households, all sizes
      "n_total": int,
      "year": fmli_year,
    }

    Parameters
    ----------
    fmli_df : pandas.DataFrame
        Must contain columns NEWID (CU-quarter id), PSU, FAM_SIZE, FINLWT21.
    mtbi_df : pandas.DataFrame
        Must contain columns NEWID, UCC, COST.
    food_cpi_annual : dict[int, float] | None
        Annual Honolulu food CPI (CUURS49ASAF11). If provided with target_year,
        each CU's quarterly FAH is inflated from fmli_year → target_year before
        weighting.
    target_year : int | None
        Year to inflate values to. Has no effect if food_cpi_annual is None.
    psu_codes : set[str] | None
        Override of HONOLULU_PSU_CODES for testing.
    """
    import pandas as pd

    psu_codes = psu_codes or HONOLULU_PSU_CODES

    fmli = fmli_df.copy()
    fmli["PSU"] = fmli["PSU"].astype(str).str.strip()
    hnl = fmli[fmli["PSU"].isin(psu_codes)].copy()
    if hnl.empty:
        return {
            "by_size": {},
            "overall": FAHResult(0.0, 0, (0.0, 0.0), "all"),
            "n_total": 0,
            "year": fmli_year,
        }

    # Aggregate FAH per CU-quarter from MTBI
    fah_mtbi = mtbi_df[mtbi_df["UCC"].astype(str).map(is_fah_ucc)].copy()
    fah_per_cu = (
        fah_mtbi.groupby("NEWID")["COST"].sum().rename("fah_quarterly").reset_index()
    )

    # Join onto Honolulu CUs (CUs with no FAH spending get 0 — preserve them)
    hnl = hnl.merge(fah_per_cu, on="NEWID", how="left")
    hnl["fah_quarterly"] = hnl["fah_quarterly"].fillna(0.0)

    # Quarterly → monthly
    hnl["fah_monthly"] = hnl["fah_quarterly"] / 3.0

    # Optional inflation adjustment
    if food_cpi_annual and target_year:
        factor = food_cpi_annual.get(target_year, 1.0) / food_cpi_annual.get(fmli_year, 1.0)
        hnl["fah_monthly"] *= factor

    # Family-size buckets
    hnl["size_bucket"] = hnl["FAM_SIZE"].map(family_size_bucket)

    def _weighted_stat(df) -> FAHResult:
        if df.empty or df["FINLWT21"].sum() == 0:
            return FAHResult(0.0, 0, (0.0, 0.0), "n/a")
        n = len(df)
        w = df["FINLWT21"].astype(float)
        v = df["fah_monthly"].astype(float)
        wsum = w.sum()
        mean = float((w * v).sum() / wsum)
        # Weighted variance for CI (approximate; use unweighted-n correction)
        var = float((w * (v - mean) ** 2).sum() / wsum)
        std = var ** 0.5
        # 95% CI on the mean using effective sample size approximation
        eff_n = (wsum ** 2) / (w ** 2).sum() if (w ** 2).sum() > 0 else n
        se = std / (eff_n ** 0.5) if eff_n > 0 else 0.0
        half = 1.96 * se
        return FAHResult(
            monthly_fah=mean,
            n_households=n,
            ci_95=(mean - half, mean + half),
            family_size_label=str(df["size_bucket"].iloc[0]) if n else "n/a",
        )

    by_size: dict[str, FAHResult] = {}
    for bucket in ("1", "2", "3", "4+"):
        sub = hnl[hnl["size_bucket"] == bucket]
        by_size[bucket] = _weighted_stat(sub)

    overall = _weighted_stat(hnl)
    overall.family_size_label = "all"

    return {
        "by_size": by_size,
        "overall": overall,
        "n_total": len(hnl),
        "year": fmli_year,
    }


# ---------------------------------------------------------------------------
# Pool multiple years
# ---------------------------------------------------------------------------
def pool_years(per_year: list[dict]) -> dict:
    """Combine per-year extract_honolulu_fah() outputs into a single estimate.

    Each year's mean is weighted by its `n_households` (precision-equivalent
    to a fixed-effects pool). 95% CI is reconstructed from the per-year half-
    widths via the standard error formula sqrt(sum(SE_i^2 * w_i^2)).
    """
    if not per_year:
        raise ValueError("pool_years: empty input")

    pooled: dict = {"by_size": {}, "overall": None, "n_total": 0, "years": []}

    # Pool overall
    overall_n = sum(yr["overall"].n_households for yr in per_year)
    if overall_n == 0:
        raise ValueError("pool_years: zero households across all years")
    pooled["n_total"] = overall_n
    pooled["years"] = [yr["year"] for yr in per_year]

    def _pool(results: list[FAHResult]) -> FAHResult:
        results = [r for r in results if r.n_households > 0]
        if not results:
            return FAHResult(0.0, 0, (0.0, 0.0), "n/a")
        n_total = sum(r.n_households for r in results)
        mean = sum(r.monthly_fah * r.n_households for r in results) / n_total
        # Pooled CI: combine each year's half-width via inverse-variance approx
        # (treat each as independent). Half = 1.96 * SE, so SE = half/1.96.
        ses = [(r.ci_95[1] - r.ci_95[0]) / 2.0 / 1.96 for r in results]
        weights = [r.n_households / n_total for r in results]
        pooled_se = (sum((s * w) ** 2 for s, w in zip(ses, weights))) ** 0.5
        half = 1.96 * pooled_se
        return FAHResult(
            monthly_fah=mean,
            n_households=n_total,
            ci_95=(mean - half, mean + half),
            family_size_label=results[0].family_size_label,
        )

    pooled["overall"] = _pool([yr["overall"] for yr in per_year])
    for bucket in ("1", "2", "3", "4+"):
        pooled["by_size"][bucket] = _pool([yr["by_size"].get(bucket, FAHResult(0, 0, (0, 0), "n/a")) for yr in per_year])

    return pooled


# ---------------------------------------------------------------------------
# Neighbor-island projection
# ---------------------------------------------------------------------------
def project_to_neighbor_islands(
    honolulu_value: float,
    basket_totals: dict[str, float],
) -> dict[str, float]:
    """Scale a Honolulu monthly figure to all five regions using the receipt-
    derived basket gradient.

    Parameters
    ----------
    honolulu_value : float
        The Honolulu monthly FAH from PUMD.
    basket_totals : dict[str, float]
        {region: receipt_basket_total} for at least Honolulu + neighbor islands.
        Region keys: "Honolulu", "Maui", "Hawaii", "Kauai" (and optionally
        "State" — if present it's overwritten with the population-weighted mean).

    Returns
    -------
    {region: monthly_estimate} for State, Honolulu, Maui, Hawaii, Kauai.
    Honolulu == honolulu_value; others scaled by basket_totals[c] / basket_totals[Honolulu].
    State is a population-weighted average of the 4 county estimates.
    """
    hnl_basket = basket_totals.get("Honolulu")
    if not hnl_basket:
        raise ValueError("basket_totals['Honolulu'] required as gradient anchor")

    out: dict[str, float] = {"Honolulu": honolulu_value}
    for county in ("Maui", "Hawaii", "Kauai"):
        bt = basket_totals.get(county)
        if bt is None or hnl_basket == 0:
            continue
        factor = bt / hnl_basket
        out[county] = honolulu_value * factor

    # Population-weighted state aggregate
    present = {c: COUNTY_POP_WEIGHTS[c] for c in ("Honolulu", "Maui", "Hawaii", "Kauai") if c in out}
    if present:
        wsum = sum(present.values())
        out["State"] = sum(out[c] * (w / wsum) for c, w in present.items())

    return out
