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
import re
import sys
from datetime import date
from pathlib import Path

import pdfplumber
import requests

# -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
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

PATCH_RE = re.compile(
    r"/\* TFP_DATA_START \*/.*?/\* TFP_DATA_END \*/",
    flags=re.DOTALL,
)


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
                ak_hi_url: str | None) -> str:
    """Render the tfpData block. Missing HI data yields a null tfpData block."""
    if hi_monthly is None or hi_period is None:
        return "/* TFP_DATA_START */\nconst tfpData = null;\n/* TFP_DATA_END */"

    ratio = round(hi_monthly / us_monthly, 3) if us_monthly else None
    us_m_str   = f"{us_monthly:.2f}" if us_monthly is not None else "null"
    us_per_str = f'"{us_period}"' if us_period else "null"
    ratio_str  = f"{ratio}" if ratio is not None else "null"
    lines = [
        "/* TFP_DATA_START */",
        "const tfpData = {",
        f'  hawaii:  {{ family4Monthly: {hi_monthly:.2f}, latestPeriod: "{hi_period}",',
        f'             source: "USDA CNPP Alaska-Hawaii Thrifty Food Plan",',
        f'             url: "{ak_hi_url or ""}" }},',
        f'  us48:    {{ family4Monthly: {us_m_str}, latestPeriod: {us_per_str},',
        f'             source: "USDA CNPP Thrifty Food Plan (US 48 avg)" }},',
        f'  hiRatio: {ratio_str},',
        f'  referenceFamily: "2 adults (20-50) + 2 children (6-8, 9-11)"',
        "};",
        "/* TFP_DATA_END */",
    ]
    return "\n".join(lines)


def patch_html(html: str, new_block: str) -> tuple[str, bool]:
    if not PATCH_RE.search(html):
        return html, False
    return PATCH_RE.sub(lambda m: new_block, html, count=1), True


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

    # --- Build and patch ---
    new_block = build_block(hi_monthly, us_monthly, hi_period, us_period, ak_hi_url)
    print("\nNew tfpData block:\n" + new_block + "\n")

    files = [Path(args.file)] if args.file else DEFAULT_FILES
    for target in files:
        if not target.exists():
            print(f"skip: {target} not found")
            continue
        html = target.read_text(encoding="utf-8")
        new_html, ok = patch_html(html, new_block)
        if not ok:
            print(f"WARNING: TFP_DATA markers not found in {target}")
            continue
        if args.dry_run:
            print(f"[dry-run] would patch {target}")
        else:
            target.write_text(new_html, encoding="utf-8")
            print(f"patched {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
