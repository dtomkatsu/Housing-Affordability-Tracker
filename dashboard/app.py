"""Hawaii Cost of Living — Grocery Price Index Dashboard.

Run with:
    streamlit run dashboard/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Allow importing from src/
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline import run_pipeline

# ---------------------------------------------------------------------------
# Page config (must be first Streamlit call)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Hawaii Cost of Living",
    page_icon="🌺",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Seafoam green palette
# ---------------------------------------------------------------------------
SEAFOAM   = "#5BBFB5"
SEAFOAM_DARK = "#2D9E94"
SEAFOAM_LIGHT = "#A8DDD9"
SEAFOAM_BG = "#EEF9F7"
TEXT_DARK  = "#1A3C3A"
TEXT_MID   = "#2D5E5A"
WHITE      = "#FFFFFF"
CORAL      = "#F4776B"

COUNTY_COLORS = {
    "honolulu": SEAFOAM_DARK,
    "maui":     SEAFOAM,
    "hawaii":   SEAFOAM_LIGHT,
    "kauai":    "#76C9C3",
}
COUNTY_LABELS = {
    "honolulu": "Honolulu",
    "maui":     "Maui",
    "hawaii":   "Hawaiʻi",
    "kauai":    "Kauaʻi",
}

# ---------------------------------------------------------------------------
# Custom CSS
# ---------------------------------------------------------------------------
st.markdown(f"""
<style>
  /* App background */
  [data-testid="stAppViewContainer"] {{
    background-color: {SEAFOAM_BG};
  }}
  /* Top header bar */
  [data-testid="stHeader"] {{
    background-color: {SEAFOAM_DARK};
  }}
  /* Sidebar */
  [data-testid="stSidebar"] {{
    background-color: {WHITE};
    border-right: 2px solid {SEAFOAM_LIGHT};
  }}
  [data-testid="stSidebar"] h1,
  [data-testid="stSidebar"] h2,
  [data-testid="stSidebar"] h3 {{
    color: {TEXT_DARK};
  }}
  /* Main headings */
  h1, h2, h3 {{
    color: {TEXT_DARK};
    font-family: 'Georgia', serif;
  }}
  /* Metric cards */
  div[data-testid="metric-container"] {{
    background: {WHITE};
    border-radius: 12px;
    border-left: 4px solid {SEAFOAM};
    padding: 12px 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.07);
  }}
  div[data-testid="metric-container"] label {{
    color: {TEXT_MID} !important;
    font-size: 0.8rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  div[data-testid="metric-container"] [data-testid="stMetricValue"] {{
    color: {TEXT_DARK} !important;
    font-size: 1.6rem;
    font-weight: 700;
  }}
  div[data-testid="metric-container"] [data-testid="stMetricDelta"] {{
    color: {SEAFOAM_DARK} !important;
  }}
  /* Section dividers */
  hr {{
    border: none;
    border-top: 2px solid {SEAFOAM_LIGHT};
    margin: 1.5rem 0;
  }}
  /* Callout box */
  .callout {{
    background: {WHITE};
    border-left: 5px solid {SEAFOAM};
    border-radius: 8px;
    padding: 16px 20px;
    margin: 12px 0;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }}
  .callout p {{
    margin: 0;
    color: {TEXT_DARK};
    font-size: 1.05rem;
  }}
  /* Hero banner */
  .hero {{
    background: linear-gradient(135deg, {SEAFOAM_DARK} 0%, {SEAFOAM} 100%);
    color: white;
    padding: 28px 32px;
    border-radius: 14px;
    margin-bottom: 24px;
  }}
  .hero h1 {{
    color: white !important;
    font-size: 2.2rem;
    margin: 0 0 6px 0;
  }}
  .hero p {{
    color: rgba(255,255,255,0.88);
    margin: 0;
    font-size: 1.05rem;
  }}
  .badge {{
    display: inline-block;
    background: rgba(255,255,255,0.22);
    border-radius: 20px;
    padding: 3px 12px;
    font-size: 0.78rem;
    color: white;
    margin-top: 10px;
  }}
  .badge-ok   {{ background: rgba(46, 160, 67, 0.88); }}
  .badge-warn {{ background: rgba(230, 150, 40, 0.92); }}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Load pipeline data (cached for 1 hour)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner="Running price pipeline…")
def load_data() -> dict:
    return run_pipeline()


data = load_data()
basket           = data["basket"]
county_prices    = data["county_prices"]
estimates        = data["estimates"]
target_date      = data["target_date"]
GET_RATE         = data["get_tax_rate"]
cpi_status       = data.get("cpi_status", {})
county_coverage  = data.get("county_coverage", {})
cov_threshold    = data.get("coverage_threshold", 0.30)

# Build DataFrames
# --- County basket totals ---
county_totals: dict[str, float] = {}
for ap in county_prices:
    county_totals[ap.county] = county_totals.get(ap.county, 0.0) + ap.adjusted_price
county_totals_tax = {c: round(v * (1 + GET_RATE), 2) for c, v in county_totals.items()}

# --- Item prices by county ---
item_rows = []
for ap in county_prices:
    item = basket.get_item(ap.slot_id)
    if item:
        item_rows.append({
            "slot_id": ap.slot_id,
            "item": item["description"],
            "category": ap.slot_id.split("-")[0],
            "county": ap.county,
            "price": ap.adjusted_price,
            "unit": item.get("norm_unit", ""),
        })
df_items = pd.DataFrame(item_rows)

# --- Household estimates ---
hh_rows = []
for est in estimates:
    hh_rows.append({
        "household_type": est.household_type,
        "label": est.household_label,
        "county": est.county,
        "pretax": est.household_cost,
        "with_tax": round(est.household_cost * (1 + GET_RATE), 2),
    })
df_hh = pd.DataFrame(hh_rows)

# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(f"### 🌺 Hawaii Cost of Living")
    st.markdown("---")

    hh_options = df_hh["label"].unique().tolist()
    selected_hh = st.selectbox(
        "Household type",
        hh_options,
        index=3,  # default: Two adults + two children
        help="Select a household composition to see estimated monthly grocery costs.",
    )

    st.markdown("---")
    categories = sorted(df_items["category"].unique().tolist())
    selected_cats = st.multiselect(
        "Filter item categories",
        categories,
        default=categories,
        help="Filter the item breakdown table by food category.",
    )

    st.markdown("---")
    if cpi_status.get("is_interpolated"):
        latest = cpi_status.get("latest_actual_period") or "n/a"
        cpi_line = f"Interpolated (latest BLS: {latest})"
    else:
        cpi_line = "Actual BLS CPI reading"
    st.caption(
        f"Data as of **{target_date.strftime('%B %Y')}**  \n"
        f"CPI: {cpi_line}  \n"
        f"Prices are member/loyalty rates  \n"
        f"+ {GET_RATE*100:.1f}% GET tax applied"
    )

# ---------------------------------------------------------------------------
# HERO BANNER
# ---------------------------------------------------------------------------
if cpi_status.get("is_interpolated"):
    _latest = cpi_status.get("latest_actual_period") or "prior period"
    cpi_badge_html = f'<span class="badge badge-warn">📈 Interpolated from {_latest}</span>'
else:
    cpi_badge_html = '<span class="badge badge-ok">✓ Actual BLS CPI</span>'

st.markdown(f"""
<div class="hero">
  <h1>🌺 Hawaii Cost of Living</h1>
  <p>Grocery Price Index by County &nbsp;·&nbsp; Member Prices + 4.5% GET Tax</p>
  <span class="badge">📅 {target_date.strftime('%B %Y')}</span>
  {cpi_badge_html}
  <span class="badge">🛒 26-Item Basket</span>
  <span class="badge">🏪 Foodland · Safeway · Walmart</span>
</div>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# SECTION 1 — County metric cards
# ---------------------------------------------------------------------------
counties_sorted = ["honolulu", "maui", "hawaii", "kauai"]
cols = st.columns(4)
honolulu_total = county_totals_tax.get("honolulu", 0)

for col, county in zip(cols, counties_sorted):
    total = county_totals_tax.get(county, 0)
    delta = round(total - honolulu_total, 2) if county != "honolulu" else None
    delta_str = f"+${delta:.2f} vs Honolulu" if delta and delta > 0 else None
    cov = county_coverage.get(county)
    if cov is not None:
        if cov < cov_threshold:
            cov_label = f"⚠️ {cov:.0%} market coverage"
        elif cov < 0.50:
            cov_label = f"🟡 {cov:.0%} market coverage"
        else:
            cov_label = f"🟢 {cov:.0%} market coverage"
    else:
        cov_label = ""
    with col:
        st.metric(
            label=COUNTY_LABELS[county],
            value=f"${total:.2f}/mo",
            delta=delta_str,
            delta_color="inverse",
        )
        if cov_label:
            st.caption(cov_label)

st.markdown("---")

# ---------------------------------------------------------------------------
# SECTION 2 — County basket comparison
# ---------------------------------------------------------------------------
st.markdown("## 📊 County Basket Comparison")

col_left, col_right = st.columns([1, 1])

with col_left:
    # Grouped bar: pre-tax and with-tax side by side
    bar_data = []
    for county in counties_sorted:
        bar_data.append({
            "County": COUNTY_LABELS[county],
            "Pre-tax": county_totals.get(county, 0),
            "With GET Tax": county_totals_tax.get(county, 0),
        })
    df_bar = pd.DataFrame(bar_data)

    fig_bar = go.Figure()
    fig_bar.add_trace(go.Bar(
        name="Pre-tax",
        x=df_bar["County"],
        y=df_bar["Pre-tax"],
        marker_color=SEAFOAM_LIGHT,
        text=[f"${v:.2f}" for v in df_bar["Pre-tax"]],
        textposition="outside",
    ))
    fig_bar.add_trace(go.Bar(
        name="With 4.5% GET Tax",
        x=df_bar["County"],
        y=df_bar["With GET Tax"],
        marker_color=SEAFOAM_DARK,
        text=[f"${v:.2f}" for v in df_bar["With GET Tax"]],
        textposition="outside",
    ))
    fig_bar.update_layout(
        barmode="group",
        title="Monthly Basket Total by County",
        yaxis_title="$ / month",
        plot_bgcolor=WHITE,
        paper_bgcolor=SEAFOAM_BG,
        font_color=TEXT_DARK,
        title_font_size=15,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=50, b=20),
        yaxis=dict(range=[0, max(county_totals_tax.values()) * 1.18]),
    )
    st.plotly_chart(fig_bar, use_container_width=True)

with col_right:
    # Heatmap: items × counties
    if not df_items.empty:
        pivot = df_items.pivot(index="item", columns="county", values="price")
        # Reorder columns
        pivot = pivot[[c for c in counties_sorted if c in pivot.columns]]
        pivot.columns = [COUNTY_LABELS[c] for c in pivot.columns]

        fig_heat = px.imshow(
            pivot,
            color_continuous_scale=[
                [0.0, WHITE],
                [0.5, SEAFOAM_LIGHT],
                [1.0, SEAFOAM_DARK],
            ],
            aspect="auto",
            title="Item Price by County ($)",
            labels=dict(color="Price ($)"),
        )
        fig_heat.update_traces(
            hovertemplate="<b>%{y}</b><br>%{x}: $%{z:.2f}<extra></extra>",
        )
        fig_heat.update_layout(
            plot_bgcolor=WHITE,
            paper_bgcolor=SEAFOAM_BG,
            font_color=TEXT_DARK,
            title_font_size=15,
            coloraxis_showscale=True,
            margin=dict(t=50, b=20, l=10, r=10),
            xaxis=dict(side="top"),
        )
        fig_heat.update_yaxes(tickfont_size=10)
        st.plotly_chart(fig_heat, use_container_width=True)

st.markdown("---")

# ---------------------------------------------------------------------------
# SECTION 3 — Household Cost Explorer
# ---------------------------------------------------------------------------
st.markdown("## 👨‍👩‍👧‍👦 Household Cost Explorer")

df_hh_sel = df_hh[df_hh["label"] == selected_hh]
df_hh_sel = df_hh_sel[df_hh_sel["county"].isin(counties_sorted)]
df_hh_sel = df_hh_sel.sort_values("county", key=lambda s: s.map({c: i for i, c in enumerate(counties_sorted)}))

fig_hh = go.Figure()
fig_hh.add_trace(go.Bar(
    x=[COUNTY_LABELS[c] for c in df_hh_sel["county"]],
    y=df_hh_sel["with_tax"],
    marker_color=[COUNTY_COLORS.get(c, SEAFOAM) for c in df_hh_sel["county"]],
    text=[f"${v:.2f}" for v in df_hh_sel["with_tax"]],
    textposition="outside",
))
fig_hh.update_layout(
    title=f"Monthly Grocery Cost — {selected_hh}",
    yaxis_title="$ / month (with GET tax)",
    plot_bgcolor=WHITE,
    paper_bgcolor=SEAFOAM_BG,
    font_color=TEXT_DARK,
    title_font_size=15,
    showlegend=False,
    margin=dict(t=50, b=20),
    yaxis=dict(range=[0, df_hh_sel["with_tax"].max() * 1.18] if not df_hh_sel.empty else [0, 200]),
)
st.plotly_chart(fig_hh, use_container_width=True)

# Callout cards — one per county
cols_hh = st.columns(4)
for col, county in zip(cols_hh, counties_sorted):
    row = df_hh_sel[df_hh_sel["county"] == county]
    if not row.empty:
        cost = row["with_tax"].values[0]
        label_short = selected_hh.split("(")[0].strip()
        with col:
            st.markdown(f"""
<div class="callout">
  <p><strong>{COUNTY_LABELS[county]}</strong><br>
  {label_short}<br>
  <span style="font-size:1.4rem;font-weight:700;color:{SEAFOAM_DARK};">${cost:.2f}</span>
  <span style="font-size:0.8rem;color:{TEXT_MID};">/mo</span></p>
</div>
""", unsafe_allow_html=True)

st.markdown("---")

# ---------------------------------------------------------------------------
# SECTION 4 — Item Breakdown Table
# ---------------------------------------------------------------------------
st.markdown("## 🛒 Item Price Breakdown")

df_filtered = df_items[df_items["category"].isin(selected_cats)] if selected_cats else df_items

if not df_filtered.empty:
    # Pivot to wide format
    pivot_tbl = df_filtered.pivot_table(
        index=["category", "item"],
        columns="county",
        values="price",
        aggfunc="mean",
    ).reset_index()

    # Reorder county columns
    county_cols = [c for c in counties_sorted if c in pivot_tbl.columns]
    pivot_tbl = pivot_tbl[["category", "item"] + county_cols]
    pivot_tbl.columns.name = None
    pivot_tbl = pivot_tbl.rename(columns={
        "category": "Category",
        "item": "Item",
        **{c: COUNTY_LABELS[c] for c in county_cols},
    })

    # Style: highlight min (seafoam) and max (coral) per row across county cols
    county_display_cols = [COUNTY_LABELS[c] for c in county_cols]

    def style_table(df: pd.DataFrame) -> pd.DataFrame:
        styled = pd.DataFrame("", index=df.index, columns=df.columns)
        for _, row_idx in enumerate(df.index):
            vals = df.loc[row_idx, county_display_cols]
            if vals.notna().any():
                min_col = vals.idxmin()
                max_col = vals.idxmax()
                styled.loc[row_idx, min_col] = f"background-color: {SEAFOAM_LIGHT}; font-weight: 600;"
                if min_col != max_col:
                    styled.loc[row_idx, max_col] = f"background-color: #FDDBD8; font-weight: 600;"
        return styled

    styled_df = (
        pivot_tbl.style
        .apply(style_table, axis=None)
        .format({col: "${:.2f}" for col in county_display_cols}, na_rep="—")
    )

    st.dataframe(styled_df, use_container_width=True, height=500)
    st.caption(f"🟢 Seafoam = cheapest county · 🔴 Pink = most expensive county per item")
else:
    st.info("No items match the selected categories.")

st.markdown("---")

# ---------------------------------------------------------------------------
# SECTION 5 — Methodology
# ---------------------------------------------------------------------------
with st.expander("📖 How this works"):
    st.markdown(f"""
**Basket**: 26 grocery items representing all USDA Thrifty Food Plan commodity groups —
grains, vegetables, fruits, dairy, proteins, fats, beverages, and prepared foods.
Hawaii-specific staples included: Calrose rice, SPAM, Dole pineapple, Maruchan ramen.

**Prices**: Member/loyalty prices only — Foodland Maika'i card, Safeway Club Card.
Neighbor island Foodland prices are collected via Instacart and deflated by a
calibration ratio derived from Honolulu in-store vs. Instacart comparisons.

**Store-share weighting**: County composites use USDA SNAP retailer data (895 Hawaii stores)
classified into chains with format-based size multipliers (Costco 3×, Supercenter 2.5×,
standard supermarket 1×). Weights validated against Census County Business Patterns employment.

**CPI adjustment**: Basket prices are updated monthly using BLS Honolulu CPI subcategories
(cereals, meats, dairy, produce, beverages, other food). Bimonthly releases are
linearly interpolated for in-between months.

**GET Tax**: Hawaii General Excise Tax of **{GET_RATE*100:.1f}%** is applied uniformly
at checkout across all counties. This is added on top of member prices.

**Household scaling**: Monthly costs are scaled from the USDA 4-person reference family
(2 adults 19-50, children 6-8 and 9-11, ~$994/month national average Jan 2025) using
USDA Thrifty Food Plan individual shares and household-size multipliers.
""")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.markdown(f"""
<div style="text-align:center; color:{TEXT_MID}; font-size:0.8rem; margin-top:2rem; padding:1rem;">
  Hawaii Cost of Living · Grocery Price Index ·
  Data: USDA SNAP, Census CBP, BLS CPI ·
  Member prices + {GET_RATE*100:.1f}% GET tax ·
  {target_date.strftime('%B %Y')}
</div>
""", unsafe_allow_html=True)
