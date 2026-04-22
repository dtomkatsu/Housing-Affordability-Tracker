#!/usr/bin/env python3
"""
gas-price-updater.py
--------------------
Scrapes current Hawaii gas prices from https://gasprices.aaa.com/?state=HI
and patches the gasData block in both squarespace-single-file.html and index.html.

Also appends a snapshot row to data/gas_prices_history.csv for time-series tracking.

Data pulled per county (regular grade only per user request):
    State, Honolulu, Maui, Hawaii, Kauai

Metro → county mapping:
    Hawaii (statewide) → State
    Honolulu            → Honolulu
    Kahului             → Maui      (same island as Wailuku; Kahului is the primary AAA label)
    Hilo                → Hawaii
    Lihue (Kauai)       → Kauai

Run:
    python3 gas-price-updater.py
    python3 gas-price-updater.py --dry-run
    python3 gas-price-updater.py --file squarespace-single-file.html
"""
from __future__ import annotations

import csv
import json
import re
import ssl
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_URL   = "https://gasprices.aaa.com/?state=HI"
HISTORY_CSV  = PROJECT_ROOT / "data" / "gas_prices_history.csv"

DEFAULT_FILES = [
    PROJECT_ROOT / "squarespace-single-file.html",
    PROJECT_ROOT / "index.html",
]

HISTORY_FIELDS = [
    "date", "region", "regular", "mid_grade", "premium", "diesel",
    "mom_change", "yoy_change", "source_url", "fetched_at",
]

# AAA metro name → HTML county key
METRO_MAP = {
    "Honolulu":       "Honolulu",
    "Kahului":        "Maui",
    "Wailuku":        "Maui",       # same island, keep as secondary check
    "Hilo":           "Hawaii",
    "Lihue (Kauai)":  "Kauai",
    "Lihue":          "Kauai",
}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"

# ------------------------------------------------------------------
# Fetch
# ------------------------------------------------------------------
def fetch_html(url: str) -> str:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ------------------------------------------------------------------
# Parse
# ------------------------------------------------------------------
TABLE_RE  = re.compile(r'<table[^>]*class="table-mob"[^>]*>(.*?)</table>', re.S)
H3_RE     = re.compile(r'<h3[^>]*>([^<]+)</h3>', re.S)
PRICE_RE  = re.compile(r'\$(\d+\.\d+)')

# Match all <td> cell texts in a table chunk
TD_RE = re.compile(r'<td[^>]*>\s*([^<]*?)\s*</td>', re.S)


def _parse_price(text: str) -> float | None:
    m = PRICE_RE.search(text)
    return float(m.group(1)) if m else None


def _parse_table(table_html: str) -> dict | None:
    """
    Rows: Current Avg, Yesterday Avg, Week Ago Avg, Month Ago Avg, Year Ago Avg
    Cols: label, Regular, Mid-Grade, Premium, Diesel
    Returns dict of { current, yesterday, week_ago, month_ago, year_ago }
    each containing { regular, mid_grade, premium, diesel }.
    """
    tds = [t.strip() for t in TD_RE.findall(table_html)]
    if len(tds) < 5:
        return None

    # Each row has 5 cells: label + 4 grades
    def get_row(label_substr: str) -> dict | None:
        for i, td in enumerate(tds):
            if label_substr.lower() in td.lower():
                chunk = tds[i:i + 5]
                if len(chunk) == 5:
                    return {
                        "regular":   _parse_price(chunk[1]),
                        "mid_grade": _parse_price(chunk[2]),
                        "premium":   _parse_price(chunk[3]),
                        "diesel":    _parse_price(chunk[4]),
                    }
        return None

    return {
        "current":    get_row("Current"),
        "yesterday":  get_row("Yesterday"),
        "week_ago":   get_row("Week Ago"),
        "month_ago":  get_row("Month Ago"),
        "year_ago":   get_row("Year Ago"),
    }


def parse_aaa_page(html: str) -> dict:
    """
    Returns: {
      "State":    { regular, mom_change, yoy_change, all_grades: {current, yesterday, ...} },
      "Honolulu": {...}, "Maui": {...}, "Hawaii": {...}, "Kauai": {...},
    }
    """
    # Split on each <h3> to find metro sections; state section is everything before first <h3>
    tables = TABLE_RE.findall(html)
    h3s    = H3_RE.findall(html)

    # Positions of tables and h3 headings in the raw html for ordering
    table_spans = [(m.start(), m.end(), m.group(1)) for m in TABLE_RE.finditer(html)]
    h3_spans    = [(m.start(), m.end(), m.group(1).strip()) for m in H3_RE.finditer(html)]

    # State table: the very first table-mob in the page
    if not table_spans:
        raise ValueError("No table.table-mob found — page structure may have changed")
    state_table_html = table_spans[0][2]
    state_rows = _parse_table(state_table_html)

    result: dict = {}

    if state_rows and state_rows["current"]:
        cur = state_rows["current"]
        mo  = state_rows["month_ago"]
        yr  = state_rows["year_ago"]
        mom = round(cur["regular"] - (mo["regular"] or 0), 3) if mo else 0.0
        yoy = round(cur["regular"] - (yr["regular"] or 0), 3) if yr else 0.0
        result["State"] = {
            "regular":     cur["regular"],
            "mid_grade":   cur["mid_grade"],
            "premium":     cur["premium"],
            "diesel":      cur["diesel"],
            "mom_change":  mom,
            "yoy_change":  yoy,
            "all_grades":  state_rows,
        }

    # Metro tables: match each <h3> to the next table-mob after it
    seen_counties: set[str] = set()
    for h3_pos, _, metro_name in h3_spans:
        county = METRO_MAP.get(metro_name.strip())
        if not county:
            continue
        if county in seen_counties:
            continue  # Kahului + Wailuku are both Maui — take first
        # Find the first table-mob that starts after this <h3>
        for t_pos, _, t_html in table_spans:
            if t_pos > h3_pos:
                rows = _parse_table(t_html)
                if rows and rows["current"]:
                    cur = rows["current"]
                    mo  = rows["month_ago"]
                    yr  = rows["year_ago"]
                    mom = round(cur["regular"] - (mo["regular"] or 0), 3) if mo else 0.0
                    yoy = round(cur["regular"] - (yr["regular"] or 0), 3) if yr else 0.0
                    result[county] = {
                        "regular":    cur["regular"],
                        "mid_grade":  cur["mid_grade"],
                        "premium":    cur["premium"],
                        "diesel":     cur["diesel"],
                        "mom_change": mom,
                        "yoy_change": yoy,
                        "all_grades": rows,
                    }
                    seen_counties.add(county)
                break

    return result


# ------------------------------------------------------------------
# History CSV
# ------------------------------------------------------------------
def append_history(data: dict, fetched_at: str) -> None:
    HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not HISTORY_CSV.exists()
    today = date.today().isoformat()
    with HISTORY_CSV.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        for region, vals in data.items():
            cur = vals.get("all_grades", {}).get("current") or {}
            mo  = vals.get("all_grades", {}).get("month_ago") or {}
            yr  = vals.get("all_grades", {}).get("year_ago") or {}
            writer.writerow({
                "date":        today,
                "region":      region,
                "regular":     cur.get("regular", ""),
                "mid_grade":   cur.get("mid_grade", ""),
                "premium":     cur.get("premium", ""),
                "diesel":      cur.get("diesel", ""),
                "mom_change":  vals.get("mom_change", ""),
                "yoy_change":  vals.get("yoy_change", ""),
                "source_url":  SOURCE_URL,
                "fetched_at":  fetched_at,
            })


# ------------------------------------------------------------------
# HTML patch
# ------------------------------------------------------------------
def _js_lit(v) -> str:
    if isinstance(v, bool):  return "true" if v else "false"
    if isinstance(v, int):   return str(v)
    if isinstance(v, float): return repr(round(v, 3))
    if isinstance(v, str):   return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):  return "[ " + ", ".join(_js_lit(x) for x in v) + " ]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k}:{_js_lit(val)}" for k, val in v.items()) + " }"
    raise TypeError(f"unsupported type {type(v)}")


def render_gas_data_block(data: dict, as_of: str) -> str:
    lines = ["/* GAS_DATA_START */", "const gasData = {"]
    order = ("State", "Honolulu", "Maui", "Hawaii", "Kauai")
    pad = max(len(c) for c in order)
    for cty in order:
        d = data.get(cty, {})
        rec = {
            "regular":    d.get("regular")    or 0.0,
            "mid_grade":  d.get("mid_grade")  or 0.0,
            "premium":    d.get("premium")     or 0.0,
            "diesel":     d.get("diesel")      or 0.0,
            "mom_change": d.get("mom_change")  or 0.0,
            "yoy_change": d.get("yoy_change")  or 0.0,
            "asOf":       as_of,
        }
        key = (cty + ":").ljust(pad + 2)
        lines.append(f"  {key}{_js_lit(rec)},")
    lines.append("};")
    lines.append("/* GAS_DATA_END */")
    return "\n".join(lines)


PATCH_RE = re.compile(
    r"/\* GAS_DATA_START \*/.*?/\* GAS_DATA_END \*/",
    flags=re.DOTALL,
)


def patch_html(html: str, new_block: str) -> tuple[str, bool]:
    if not PATCH_RE.search(html):
        return html, False
    return PATCH_RE.sub(lambda _: new_block, html, count=1), True


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main() -> int:
    dry_run  = "--dry-run" in sys.argv
    file_arg = sys.argv[sys.argv.index("--file") + 1] if "--file" in sys.argv else None
    files: list[Path] = [Path(file_arg)] if file_arg else DEFAULT_FILES

    print(f"Fetching Hawaii gas prices from AAA…")
    try:
        html = fetch_html(SOURCE_URL)
    except Exception as exc:
        print(f"  ERROR fetching {SOURCE_URL}: {exc}", file=sys.stderr)
        return 1

    data = parse_aaa_page(html)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    as_of = date.today().isoformat()

    if not data:
        print("  ERROR: no price data extracted — page structure may have changed.", file=sys.stderr)
        return 1

    for cty in ("State", "Honolulu", "Maui", "Hawaii", "Kauai"):
        d = data.get(cty, {})
        reg = d.get("regular")
        mom = d.get("mom_change", 0)
        print(f"  {cty:9s} regular=${reg or 'N/A'}  "
              f"MoM={'+' if mom >= 0 else ''}{mom:.3f}  "
              f"YoY={'+' if d.get('yoy_change',0)>=0 else ''}{d.get('yoy_change',0):.3f}")

    new_block = render_gas_data_block(data, as_of)

    if dry_run:
        print("\n--- DRY RUN: block to be written ---")
        print(new_block)
        return 0

    # Append to history
    append_history(data, fetched_at)
    print(f"  History appended → {HISTORY_CSV.relative_to(PROJECT_ROOT)}")

    # Patch HTML files
    for path in files:
        if not path.exists():
            print(f"  skipping {path.name} (not found)")
            continue
        old_html = path.read_text()
        new_html, ok = patch_html(old_html, new_block)
        if not ok:
            print(f"  WARNING: no GAS_DATA markers found in {path.name} — add them first")
            continue
        if new_html == old_html:
            print(f"  {path.name}: no change")
        else:
            path.write_text(new_html)
            print(f"  {path.name}: updated ✓")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
