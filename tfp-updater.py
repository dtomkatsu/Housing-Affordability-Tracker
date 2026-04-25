#!/usr/bin/env python3
"""
tfp-updater.py
--------------
Fetches the USDA Thrifty Food Plan (TFP) monthly cost for the reference
family of four in Hawaiʻi from the USDA-FNS CNPP "Cost of Food at Home"
reports, and patches the `tfpData` block in both HTML files.

Reference family (per 7 U.S.C. § 2012):
  - Male 20–50 years + Female 20–50 years
  - Two children, ages 6–8 and 9–11

Data sources:
  AK-HI PDF: https://www.fns.usda.gov/sites/default/files/resource-files/cnpp-costfood-alaskahawaii-<mon><yyyy>.pdf
  US48 PDF:  https://www.fns.usda.gov/sites/default/files/resource-files/cnpp-costfood-tfp-<mon><yyyy>.pdf
  Index:     https://www.fns.usda.gov/research/cnpp/usda-food-plans/cost-food-monthly-reports

Both PDFs contain a single row shaped like:
  "... and Two Children, 6–8 and 9–11 years $1,295.20 $1,529.60"

Patch strategy: replace everything between
  /* TFP_DATA_START */ ... /* TFP_DATA_END */
markers in the HTML.

Run:
  python3 tfp-updater.py
  python3 tfp-updater.py --dry-run
  python3 tfp-updater.py --pdf path/to/local.pdf --pdf-us path/to/national.pdf
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import pdfplumber
import requests

# -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
from common.html_patcher import patch_html_files  # noqa: E402
DEFAULT_FILES = [
    PROJECT_ROOT / "squarespace-single-file.html",
    PROJECT_ROOT / "index.html",
]

INDEX_URL = "https://www.fns.usda.gov/research/cnpp/usda-food-plans/cost-food-monthly-reports"
PDF_BASE = "https://www.fns.usda.gov/sites/default/files/resource-files"

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTHS_LONG = ["january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]

# Matches the "and Two Children, 6–8 and 9–11 years $X,XXX.XX $Y,YYY.YY" line.
# Hyphen variants: U+002D (-), U+2013 (–), U+2014 (—). Comma in dollar amount optional.
REFROW_RE = re.compile(
    r"Two\s+Children[^\$\n]*\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})",
    re.IGNORECASE,
)

_DATA_TAG = "TFP"

# BLS series for forward-projection: Honolulu "Food at home"
BLS_API_URL     = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_FOOD_SERIES = "CUURS49ASAF11"  # Honolulu, food at home, not seasonally adjusted


# -----------------------------------------------------------------
def fetch_bls_food_cpi(start_year: int, end_year: int) -> list[dict] | None:
    """Fetch monthly Honolulu food-at-home CPI points between start_year and
    end_year inclusive. Returns a list of {year, period, value} dicts sorted
    ascending, or None on any failure (caller falls back to raw TFP value).
    """
    api_key = os.environ.get("BLS_API_KEY", "")
    payload: dict = {
        "seriesid": [BLS_FOOD_SERIES],
        "startyear": str(start_year),
        "endyear":   str(end_year),
    }
    if api_key:
        payload["registrationkey"] = api_key
    try:
        resp = requests.post(
            BLS_API_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            return None
        series = data.get("Results", {}).get("series", [])
        if not series:
            return None
        points = []
        for obs in series[0].get("data", []):
            raw_val = obs.get("value", "-")
            if raw_val == "-":
                continue
            if not obs["period"].startswith("M") or obs["period"] == "M13":
                continue
            points.append({
                "year":   int(obs["year"]),
                "month":  int(obs["period"][1:]),
                "value":  float(raw_val),
            })
        points.sort(key=lambda p: (p["year"], p["month"]))
        return points or None
    except Exception:
        return None


# Per-month projection cap: bounds noisy bimonthly print from compounding
# into an unrealistic extrapolation. (1+0.0189)^12 ≈ 1.252 → ±~25%/yr.
# Mirrors the cap used in pipelines/grocery/src/price_adjuster.py so all
# CPI-driven projections in this repo share the same momentum ceiling.
_PROJ_MONTHLY_CAP = 0.0189


def _cpi_value_for(points: list[dict], year: int, month: int) -> float | None:
    """Return the CPI value at (year, month).

    If the exact (year, month) is missing — common for bimonthly Honolulu CPI
    where data lands only in odd months — interpolate linearly between the
    bracketing observations. If the target is *past* the latest observation,
    forward-project using the compound monthly rate from the last two points
    (capped at ±_PROJ_MONTHLY_CAP/month). Returns None if the series has no
    points at all.

    The previous version returned the nearest *earlier* observation, which
    silently flat-lined any reference month past the latest BLS print. That
    masked the case where TFP was 1–2 months stale relative to the dashboard's
    reference month, hiding what is conceptually a forward projection.
    """
    if not points:
        return None
    target = (year, month)
    ordered = sorted(points, key=lambda p: (p["year"], p["month"]))

    # Exact match.
    for p in ordered:
        if (p["year"], p["month"]) == target:
            return p["value"]

    # Bracketing observations (interpolation).
    before = None
    after = None
    for p in ordered:
        pt = (p["year"], p["month"])
        if pt < target and (before is None or pt > (before["year"], before["month"])):
            before = p
        elif pt > target and (after is None or pt < (after["year"], after["month"])):
            after = p

    if before is None and after is None:
        return None
    if before is None:
        # Target precedes every observation — return the earliest as a
        # least-bad guess (rare; would only occur if BLS hasn't backfilled).
        return ordered[0]["value"]
    if after is not None:
        # Linear interpolation in month-index space.
        b_idx = before["year"] * 12 + before["month"]
        a_idx = after["year"]  * 12 + after["month"]
        t_idx = year * 12 + month
        span = a_idx - b_idx
        if span == 0:
            return before["value"]
        frac = (t_idx - b_idx) / span
        return before["value"] + frac * (after["value"] - before["value"])

    # Target is past the latest observation — forward-project.
    if len(ordered) < 2 or before["value"] <= 0:
        return before["value"]
    prev = ordered[-2]
    months_between = (before["year"] - prev["year"]) * 12 + (before["month"] - prev["month"])
    months_beyond  = (year - before["year"]) * 12 + (month - before["month"])
    if months_between <= 0 or prev["value"] <= 0 or months_beyond <= 0:
        return before["value"]
    monthly_rate = (before["value"] / prev["value"]) ** (1.0 / months_between) - 1.0
    monthly_rate = max(min(monthly_rate, _PROJ_MONTHLY_CAP), -_PROJ_MONTHLY_CAP)
    return before["value"] * (1.0 + monthly_rate) ** months_beyond


def reference_month(today: date | None = None) -> tuple[int, int]:
    """Return (year, month) of the current reference month.

    Scraper runs on day 22 (after Redfin's 3rd-Friday release), so the
    reference month is *last* calendar month. Before day 22, fall back to
    two months ago since current-month Redfin data isn't available yet.
    """
    today = today or date.today()
    y, m = today.year, today.month
    if today.day < 22:
        m -= 1
    m -= 1  # Redfin data always one month behind run date
    if m == 0:
        m = 12
        y -= 1
    elif m == -1:
        m = 11
        y -= 1
    return y, m


def project_tfp_forward(hi_monthly: float, hi_period: str,
                        ref_year: int, ref_month: int) -> tuple[float, str, float] | None:
    """Forward-project a TFP value from hi_period to reference month via
    BLS Honolulu food-at-home CPI ratio.

    Returns (projected_monthly, cpi_period_used, ratio) or None on failure.
    """
    try:
        tfp_y, tfp_m = int(hi_period[:4]), int(hi_period[5:7])
    except (ValueError, IndexError):
        return None

    # No projection needed if TFP is already at/after reference month
    if (tfp_y, tfp_m) >= (ref_year, ref_month):
        return None

    # Fetch CPI spanning both periods
    start_year = min(tfp_y, ref_year) - 1  # 1-yr buffer for carry-forward
    end_year   = max(tfp_y, ref_year)
    points = fetch_bls_food_cpi(start_year, end_year)
    if not points:
        return None

    tfp_cpi = _cpi_value_for(points, tfp_y, tfp_m)
    ref_cpi = _cpi_value_for(points, ref_year, ref_month)
    if tfp_cpi is None or ref_cpi is None or tfp_cpi == 0:
        return None

    ratio       = ref_cpi / tfp_cpi
    projected   = hi_monthly * ratio
    cpi_period  = f"{ref_year}-{ref_month:02d}"
    return projected, cpi_period, ratio


# -----------------------------------------------------------------
def try_fetch(url: str, timeout: int = 30) -> bytes | None:
    """GET url and return bytes if status 200, else None."""
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and r.content:
            return r.content
    except requests.RequestException:
        pass
    return None


def fetch_pdf_by_slug(slug_prefix: str, today: date | None = None) -> tuple[bytes, str] | None:
    """Walk back through recent months trying slug variants.

    slug_prefix is 'cnpp-costfood-alaskahawaii' or 'cnpp-costfood-tfp'.
    Returns (pdf_bytes, source_url) or None.
    """
    if today is None:
        today = date.today()
    y, m = today.year, today.month
    for _ in range(6):  # try up to 6 months back
        mon_short = MONTHS[m - 1]
        mon_long = MONTHS_LONG[m - 1]
        for slug_mon in [mon_short, mon_short.capitalize(), mon_long, mon_long.capitalize()]:
            url = f"{PDF_BASE}/{slug_prefix}-{slug_mon}{y}.pdf"
            body = try_fetch(url)
            if body:
                return body, url
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return None


def fetch_pdf_via_index(slug_prefix: str) -> tuple[bytes, str] | None:
    """Fallback: scrape the CNPP index page for the latest matching PDF href."""
    body = try_fetch(INDEX_URL)
    if not body:
        return None
    html = body.decode("utf-8", errors="replace")
    # Find all hrefs matching the slug prefix and dated with month+year.
    pattern = re.compile(rf'href="([^"]*{re.escape(slug_prefix)}[^"]*\.pdf)"', re.IGNORECASE)
    hrefs = pattern.findall(html)
    if not hrefs:
        return None
    # Score each href by embedded year then month-order; pick the highest.
    def score(href: str) -> tuple[int, int]:
        h = href.lower()
        m_year = re.search(r"(20\d{2})", h)
        yr = int(m_year.group(1)) if m_year else 0
        mo = 0
        for i, name in enumerate(MONTHS):
            if name in h or MONTHS_LONG[i] in h:
                mo = i + 1
                break
        return (yr, mo)
    hrefs.sort(key=score, reverse=True)
    for href in hrefs:
        url = href if href.startswith("http") else f"https://www.fns.usda.gov{href}"
        body = try_fetch(url)
        if body:
            return body, url
    return None


def fetch_pdf(slug_prefix: str) -> tuple[bytes, str] | None:
    """Try date-walked slugs first, then fall back to index scrape."""
    result = fetch_pdf_by_slug(slug_prefix)
    if result:
        return result
    return fetch_pdf_via_index(slug_prefix)


def parse_pdf_text(pdf_bytes: bytes) -> str:
    """Extract full first-page text."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return pdf.pages[0].extract_text() or ""


def parse_values(text: str) -> tuple[float, float] | None:
    """Return (first_col, second_col) dollar values from the reference-family row.

    AK-HI PDF has two columns: (Anchorage, Hawaii).
    National PDF has two columns: (Weekly, Monthly).
    """
    m = REFROW_RE.search(text)
    if not m:
        return None
    try:
        v1 = float(m.group(1).replace(",", ""))
        v2 = float(m.group(2).replace(",", ""))
        return v1, v2
    except ValueError:
        return None


def parse_period(text: str) -> str | None:
    """Extract 'YYYY-MM' from text like 'Alaska and Hawaii, January 2026 1'."""
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})",
                  text, re.IGNORECASE)
    if not m:
        return None
    mon_name = m.group(1).lower()
    try:
        mo = MONTHS_LONG.index(mon_name) + 1
    except ValueError:
        return None
    return f"{int(m.group(2))}-{mo:02d}"


# -----------------------------------------------------------------
def build_block(hi_monthly: float | None, us_monthly: float | None,
                hi_period: str | None, us_period: str | None,
                ak_hi_url: str | None,
                projection: tuple[float, str, float] | None = None,
                original_period: str | None = None) -> str:
    """Render the tfpData block. Missing HI data yields a null tfpData block.

    When `projection` is provided (tuple of projected_monthly, cpi_ref_period,
    ratio), the emitted block uses the projected value and carries
    `projected: true` + `originalPeriod` + `projectionNote` fields so the
    dashboard can display a "proj." tag.
    """
    if hi_monthly is None or hi_period is None:
        return "/* TFP_DATA_START */\nconst tfpData = null;\n/* TFP_DATA_END */"

    # If projection applied: the emitted `family4Monthly` is the projected
    # value, `latestPeriod` is the reference month (projected-to), and
    # originalPeriod / projected / projectionNote describe the lineage.
    if projection is not None:
        projected_monthly, ref_period, ratio = projection
        display_monthly   = projected_monthly
        display_period    = ref_period
        projected_flag    = "true"
        orig_period_str   = original_period or hi_period
        projection_note   = (
            f'scaled from {orig_period_str} via BLS Honolulu food CPI '
            f'(ratio {ratio:.4f})'
        )
    else:
        display_monthly   = hi_monthly
        display_period    = hi_period
        projected_flag    = "false"
        orig_period_str   = hi_period
        projection_note   = ""

    hi_ratio_vs_us = round(display_monthly / us_monthly, 3) if us_monthly else None
    us_m_str   = f"{us_monthly:.2f}" if us_monthly is not None else "null"
    us_per_str = f'"{us_period}"' if us_period else "null"
    ratio_str  = f"{hi_ratio_vs_us}" if hi_ratio_vs_us is not None else "null"

    # Build Hawaii block fields conditionally (only emit projection fields when applied)
    hawaii_fields = [
        f'family4Monthly: {display_monthly:.2f}',
        f'latestPeriod: "{display_period}"',
        f'originalPeriod: "{orig_period_str}"',
        f'projected: {projected_flag}',
    ]
    if projection_note:
        hawaii_fields.append(f'projectionNote: "{projection_note}"')
    hawaii_fields.extend([
        f'source: "USDA CNPP Alaska-Hawaii Thrifty Food Plan"',
        f'url: "{ak_hi_url or ""}"',
    ])

    lines = [
        "/* TFP_DATA_START */",
        "const tfpData = {",
        "  hawaii:  { " + ", ".join(hawaii_fields) + " },",
        f'  us48:    {{ family4Monthly: {us_m_str}, latestPeriod: {us_per_str},',
        f'             source: "USDA CNPP Thrifty Food Plan (US 48 avg)" }},',
        f'  hiRatio: {ratio_str},',
        f'  referenceFamily: "2 adults (20-50) + 2 children (6-8, 9-11)"',
        "};",
        "/* TFP_DATA_END */",
    ]
    return "\n".join(lines)


# -----------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Update USDA Thrifty Food Plan data block in dashboard HTML")
    ap.add_argument("--dry-run", action="store_true", help="Print block without writing")
    ap.add_argument("--pdf", help="Local AK-HI PDF path (skip download)")
    ap.add_argument("--pdf-us", help="Local national PDF path (skip download)")
    ap.add_argument("--file", help="Single HTML file to patch (default: both squarespace + index)")
    args = ap.parse_args()

    # --- Fetch / load AK-HI PDF ---
    hi_monthly: float | None = None
    hi_period: str | None = None
    ak_hi_url: str | None = None
    try:
        if args.pdf:
            pdf_bytes = Path(args.pdf).read_bytes()
            ak_hi_url = f"file://{args.pdf}"
        else:
            got = fetch_pdf("cnpp-costfood-alaskahawaii")
            if not got:
                raise RuntimeError("could not fetch AK-HI PDF (tried slug variants + index fallback)")
            pdf_bytes, ak_hi_url = got
        text = parse_pdf_text(pdf_bytes)
        vals = parse_values(text)
        if vals:
            _anchorage, hi_monthly = vals
        hi_period = parse_period(text)
        print(f"AK-HI: Hawaii ${hi_monthly} · period {hi_period} · url {ak_hi_url}")
    except Exception as e:
        print(f"WARNING: AK-HI fetch/parse failed: {e}")

    # --- Fetch / load national PDF ---
    us_monthly: float | None = None
    us_period: str | None = None
    try:
        if args.pdf_us:
            pdf_bytes2 = Path(args.pdf_us).read_bytes()
            us_url = f"file://{args.pdf_us}"
        else:
            got = fetch_pdf("cnpp-costfood-tfp")
            if not got:
                raise RuntimeError("could not fetch national PDF")
            pdf_bytes2, us_url = got
        print(f"US URL: {us_url}")
        text2 = parse_pdf_text(pdf_bytes2)
        vals2 = parse_values(text2)
        if vals2:
            _weekly, us_monthly = vals2
        us_period = parse_period(text2)
        print(f"US48:  Monthly ${us_monthly} · period {us_period}")
    except Exception as e:
        print(f"WARNING: national fetch/parse failed: {e}")

    # --- Forward-project HI value to reference month via BLS food CPI ---
    projection = None
    if hi_monthly is not None and hi_period:
        ref_y, ref_m = reference_month()
        print(f"\nReference month for projection: {ref_y}-{ref_m:02d}")
        projection = project_tfp_forward(hi_monthly, hi_period, ref_y, ref_m)
        if projection:
            pm, cpi_per, ratio = projection
            print(f"  Projected TFP HI: ${hi_monthly:.2f} → ${pm:.2f} "
                  f"(×{ratio:.4f} via food CPI {cpi_per})")
        else:
            print("  No projection applied (raw TFP period ≥ ref month, or BLS fetch failed)")

    # --- Build and patch ---
    new_block = build_block(hi_monthly, us_monthly, hi_period, us_period, ak_hi_url,
                            projection=projection, original_period=hi_period)
    print("\nNew tfpData block:\n" + new_block + "\n")

    files = [Path(args.file)] if args.file else DEFAULT_FILES
    patch_html_files(files, _DATA_TAG, new_block, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
