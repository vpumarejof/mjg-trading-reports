#!/usr/bin/env python3
"""
MJG Trading — Weekly Brand Intelligence & Reorder Report
Runs every Friday at 8 AM EST via GitHub Actions.
"""

import base64
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shopify_client import ShopifyClient
from email_utils import send_email

EST = timezone(timedelta(hours=-5))
LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo.jpg"

STATUS_ORDER = ["REORDER NOW", "REORDER SOON", "SOLD OUT", "MONITOR", "ADEQUATE", "OVERSTOCKED"]
STATUS_STYLE = {
    "REORDER NOW":  "font-weight:700;color:#b91c1c",
    "REORDER SOON": "font-weight:600;color:#92400e",
    "SOLD OUT":     "font-weight:600;color:#374151",
    "MONITOR":      "color:#6b7280;font-style:italic",
    "ADEQUATE":     "color:#166534",
    "OVERSTOCKED":  "color:#1e3a5f",
}


def load_logo_b64():
    if LOGO_PATH.exists():
        return base64.b64encode(LOGO_PATH.read_bytes()).decode()
    return None


def build_brand_data(collections, orders_this_week, orders_last_week):
    def tally(orders):
        units, revenue = {}, {}
        for order in orders:
            for item in order["lineItems"]["nodes"]:
                if not item.get("product"):
                    continue
                pid = item["product"]["legacyResourceId"]
                qty = item["quantity"]
                price = float(item["originalUnitPriceSet"]["shopMoney"]["amount"]) * qty
                units[pid] = units.get(pid, 0) + qty
                revenue[pid] = revenue.get(pid, 0.0) + price
        return units, revenue

    units_tw, rev_tw = tally(orders_this_week)
    units_lw, rev_lw = tally(orders_last_week)

    brands = []
    for col in sorted(collections, key=lambda c: c["title"]):
        active = [p for p in col["products"]["nodes"] if p["status"] == "ACTIVE"]
        if not active:
            continue

        stock = sum(
            max(0, v["inventoryQuantity"] or 0)
            for p in active for v in p["variants"]["nodes"]
        )
        u_tw = sum(units_tw.get(p["legacyResourceId"], 0) for p in active)
        u_lw = sum(units_lw.get(p["legacyResourceId"], 0) for p in active)
        r_tw = sum(rev_tw.get(p["legacyResourceId"], 0.0) for p in active)
        r_lw = sum(rev_lw.get(p["legacyResourceId"], 0.0) for p in active)

        avg_daily = u_tw / 7.0
        if stock == 0:
            status = "SOLD OUT"
        elif avg_daily == 0:
            status = "MONITOR"
        else:
            days = stock / avg_daily
            if days < 7:
                status = "REORDER NOW"
            elif days < 14:
                status = "REORDER SOON"
            elif days < 45:
                status = "ADEQUATE"
            else:
                status = "OVERSTOCKED"

        if u_lw == 0:
            trend = f"+{u_tw}" if u_tw > 0 else "—"
        elif u_tw > u_lw:
            trend = f"+{round(((u_tw - u_lw) / u_lw) * 100)}%"
        elif u_tw < u_lw:
            trend = f"−{round(((u_lw - u_tw) / u_lw) * 100)}%"
        else:
            trend = "="

        days_of_stock = None if avg_daily == 0 else round(stock / avg_daily)

        brands.append({
            "title": col["title"],
            "legacy_id": col["legacyResourceId"],
            "product_count": len(active),
            "stock": stock,
            "u_tw": u_tw,
            "u_lw": u_lw,
            "r_tw": r_tw,
            "r_lw": r_lw,
            "days_of_stock": days_of_stock,
            "trend": trend,
            "status": status,
        })

    return sorted(brands, key=lambda b: (STATUS_ORDER.index(b["status"]), -b["u_tw"]))


def build_top_products(orders_this_week):
    products = {}
    for order in orders_this_week:
        for item in order["lineItems"]["nodes"]:
            if not item.get("product"):
                continue
            pid = item["product"]["legacyResourceId"]
            if pid not in products:
                products[pid] = {
                    "title": item["product"]["title"],
                    "legacy_id": pid,
                    "units": 0,
                    "revenue": 0.0,
                }
            products[pid]["units"] += item["quantity"]
            products[pid]["revenue"] += (
                float(item["originalUnitPriceSet"]["shopMoney"]["amount"]) * item["quantity"]
            )
    return sorted(products.values(), key=lambda x: x["units"], reverse=True)[:10]


def executive_summary(orders_tw, orders_lw):
    def agg(orders):
        rev = sum(float(o["totalPriceSet"]["shopMoney"]["amount"]) for o in orders)
        cnt = len(orders)
        return rev, cnt, rev / cnt if cnt else 0

    r_tw, c_tw, aov_tw = agg(orders_tw)
    r_lw, c_lw, aov_lw = agg(orders_lw)

    def chg(a, b):
        return ((a - b) / b * 100) if b else None

    return {
        "r_tw": r_tw, "r_lw": r_lw, "r_chg": chg(r_tw, r_lw),
        "c_tw": c_tw, "c_lw": c_lw, "c_chg": chg(c_tw, c_lw),
        "aov_tw": aov_tw, "aov_lw": aov_lw, "aov_chg": chg(aov_tw, aov_lw),
    }


def fmt_chg(val):
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    color = "#166534" if val >= 0 else "#b91c1c"
    return f'<span style="color:{color};font-size:12px">{sign}{val:.1f}%</span>'


def fmt_money(v):
    return f"${v:,.2f}"


def admin_col_url(lid):
    return f"https://business-mjgtrading.myshopify.com/admin/collections/{lid}"


def admin_prod_url(lid):
    return f"https://business-mjgtrading.myshopify.com/admin/products/{lid}"


def build_email(collections, orders_tw, orders_lw, now_est):
    logo_b64 = load_logo_b64()
    logo_tag = (
        f'<img src="data:image/jpeg;base64,{logo_b64}" alt="MJG Trading" '
        f'style="width:80px;height:80px;object-fit:contain;display:block">'
        if logo_b64 else '<span style="font-size:20px;font-weight:700;color:#fff">MJG Trading</span>'
    )

    week_end = now_est.strftime("%B %d, %Y")
    week_start = (now_est - timedelta(days=6)).strftime("%B %d")
    brands = build_brand_data(collections, orders_tw, orders_lw)
    top_products = build_top_products(orders_tw)
    summary = executive_summary(orders_tw, orders_lw)

    urgent_brands = [b for b in brands if b["status"] in ("REORDER NOW", "SOLD OUT")]
    dead_brands = [b for b in brands if b["status"] == "MONITOR" and b["u_lw"] == 0]

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body{{font-family:'Segoe UI',Arial,sans-serif;color:#1f2937;max-width:860px;margin:0 auto;padding:0;background:#f3f4f6}}
  .wrapper{{background:#fff;max-width:860px;margin:0 auto}}
  .header{{background:#ffffff;border-bottom:1px solid #e5e7eb;padding:24px 32px;display:flex;align-items:center;justify-content:space-between}}
  .header-right{{text-align:right;color:#6b7280;font-size:13px;line-height:1.6}}
  .header-right strong{{color:#0f172a;font-size:15px;display:block}}
  .body{{padding:28px 32px}}
  h2{{font-size:14px;font-weight:700;color:#0f172a;text-transform:uppercase;letter-spacing:.06em;margin:28px 0 10px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}}
  .kpi-row{{display:flex;gap:12px;margin-bottom:4px}}
  .kpi{{flex:1;background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:16px 18px}}
  .kpi-num{{font-size:26px;font-weight:700;color:#0f172a}}
  .kpi-label{{font-size:11px;color:#6b7280;margin-top:2px;text-transform:uppercase;letter-spacing:.04em}}
  .kpi-sub{{font-size:12px;color:#9ca3af;margin-top:4px}}
  table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:4px}}
  th{{background:#0f172a;color:#e2e8f0;padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}}
  td{{padding:9px 12px;border-bottom:1px solid #f1f5f9;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#f8fafc}}
  a{{color:#1e40af;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  .footer{{background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 32px;font-size:11px;color:#9ca3af;text-align:center}}
  .alert-box{{background:#fef2f2;border-left:3px solid #dc2626;padding:12px 16px;margin-bottom:16px;border-radius:0 4px 4px 0;font-size:13px}}
  .alert-box strong{{color:#991b1b}}
  .badge{{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:3px;border:1px solid currentColor}}
</style>
</head>
<body>
<div class="wrapper">

<div class="header">
  <div>{logo_tag}</div>
  <div class="header-right">
    <strong>Weekly Brand Intelligence Report</strong>
    {week_start} – {week_end}
  </div>
</div>

<div class="body">

<h2>Executive Summary</h2>
<div class="kpi-row">
  <div class="kpi">
    <div class="kpi-num">{fmt_money(summary['r_tw'])}</div>
    <div class="kpi-label">Revenue This Week</div>
    <div class="kpi-sub">vs {fmt_money(summary['r_lw'])} last week &nbsp;{fmt_chg(summary['r_chg'])}</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{summary['c_tw']}</div>
    <div class="kpi-label">Orders</div>
    <div class="kpi-sub">vs {summary['c_lw']} last week &nbsp;{fmt_chg(summary['c_chg'])}</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{fmt_money(summary['aov_tw'])}</div>
    <div class="kpi-label">Avg Order Value</div>
    <div class="kpi-sub">vs {fmt_money(summary['aov_lw'])} last week &nbsp;{fmt_chg(summary['aov_chg'])}</div>
  </div>
</div>
"""

    if urgent_brands:
        items_html = ", ".join(
            f'<a href="{admin_col_url(b["legacy_id"])}">{b["title"]}</a>'
            for b in urgent_brands
        )
        html += f"""
<div class="alert-box" style="margin-top:20px">
  <strong>⚠ Reorder Alert ({len(urgent_brands)} brand{"s" if len(urgent_brands)>1 else ""}):</strong>
  &nbsp;{items_html} — stock critically low or sold out.
</div>"""

    html += """
<h2>Brand Reorder Status</h2>
<table>
<tr>
  <th>Brand</th>
  <th style="text-align:right">In Stock</th>
  <th style="text-align:right">Sold This Wk</th>
  <th style="text-align:right">Sold Last Wk</th>
  <th style="text-align:center">Trend</th>
  <th style="text-align:right">Days Left</th>
  <th>Status</th>
</tr>
"""

    for b in brands:
        days_disp = str(b["days_of_stock"]) if b["days_of_stock"] is not None else "—"
        style = STATUS_STYLE.get(b["status"], "")
        html += f"""<tr>
  <td><a href="{admin_col_url(b['legacy_id'])}">{b['title']}</a>
    <span style="color:#9ca3af;font-size:11px">&nbsp;({b['product_count']} SKUs)</span></td>
  <td style="text-align:right">{b['stock']:,}</td>
  <td style="text-align:right">{b['u_tw']}</td>
  <td style="text-align:right">{b['u_lw']}</td>
  <td style="text-align:center;color:#374151">{b['trend']}</td>
  <td style="text-align:right">{days_disp}</td>
  <td style="{style}">{b['status']}</td>
</tr>"""

    html += "</table>"

    html += """
<h2>Top 10 Products This Week</h2>
<table>
<tr>
  <th>#</th><th>Product</th>
  <th style="text-align:right">Units Sold</th>
  <th style="text-align:right">Revenue</th>
</tr>
"""
    if top_products:
        for i, p in enumerate(top_products, 1):
            html += f"""<tr>
  <td style="color:#9ca3af;font-weight:600">{i}</td>
  <td><a href="{admin_prod_url(p['legacy_id'])}">{p['title']}</a></td>
  <td style="text-align:right;font-weight:600">{p['units']}</td>
  <td style="text-align:right">{fmt_money(p['revenue'])}</td>
</tr>"""
    else:
        html += '<tr><td colspan="4" style="color:#9ca3af;font-style:italic">No sales recorded this week.</td></tr>'
    html += "</table>"

    html += """
<h2>Brand Revenue Ranking</h2>
<table>
<tr>
  <th>Brand</th>
  <th style="text-align:right">Revenue This Wk</th>
  <th style="text-align:right">Revenue Last Wk</th>
  <th style="text-align:center">Change</th>
</tr>
"""
    ranked = sorted([b for b in brands if b["r_tw"] > 0 or b["r_lw"] > 0], key=lambda x: -x["r_tw"])
    if ranked:
        for b in ranked:
            chg = None
            if b["r_lw"] > 0:
                chg = (b["r_tw"] - b["r_lw"]) / b["r_lw"] * 100
            html += f"""<tr>
  <td><a href="{admin_col_url(b['legacy_id'])}">{b['title']}</a></td>
  <td style="text-align:right;font-weight:600">{fmt_money(b['r_tw'])}</td>
  <td style="text-align:right;color:#9ca3af">{fmt_money(b['r_lw'])}</td>
  <td style="text-align:center">{fmt_chg(chg)}</td>
</tr>"""
    else:
        html += '<tr><td colspan="4" style="color:#9ca3af;font-style:italic">No revenue data for this period.</td></tr>'
    html += "</table>"

    html += "<h2>Flags & Recommendations</h2>"

    if dead_brands:
        html += '<p style="font-size:13px;color:#374151;margin:0 0 8px"><strong>No sales in 2+ weeks:</strong> '
        html += ", ".join(
            f'<a href="{admin_col_url(b["legacy_id"])}">{b["title"]}</a>' for b in dead_brands
        )
        html += " — consider promotional push or inventory review.</p>"

    no_stock_any_sales = [b for b in brands if b["status"] == "SOLD OUT" and b["u_tw"] > 0]
    if no_stock_any_sales:
        html += '<p style="font-size:13px;color:#374151;margin:0 0 8px"><strong>Sold out with demand:</strong> '
        html += ", ".join(
            f'<a href="{admin_col_url(b["legacy_id"])}">{b["title"]}</a> ({b["u_tw"]} units sold)'
            for b in no_stock_any_sales
        )
        html += " — high priority reorder.</p>"

    top_brand = max(brands, key=lambda b: b["r_tw"], default=None)
    if top_brand and top_brand["r_tw"] > 0:
        html += f'<p style="font-size:13px;color:#374151;margin:0 0 8px"><strong>Best performing brand:</strong> '
        html += f'<a href="{admin_col_url(top_brand["legacy_id"])}">{top_brand["title"]}</a> '
        html += f'with {fmt_money(top_brand["r_tw"])} in revenue and {top_brand["u_tw"]} units sold.</p>'

    growing = [b for b in brands if b["u_lw"] > 0 and b["u_tw"] > b["u_lw"] * 1.5]
    if growing:
        html += '<p style="font-size:13px;color:#374151;margin:0 0 8px"><strong>Accelerating demand (+50% vs last week):</strong> '
        html += ", ".join(
            f'<a href="{admin_col_url(b["legacy_id"])}">{b["title"]}</a> ({b["trend"]})'
            for b in growing
        )
        html += " — monitor stock levels.</p>"

    if not any([dead_brands, no_stock_any_sales, top_brand, growing]):
        html += '<p style="color:#9ca3af;font-style:italic;font-size:13px">No significant flags this week.</p>'

    html += f"""
</div>
<div class="footer">
  MJG Trading &nbsp;·&nbsp; Weekly Brand Intelligence Report &nbsp;·&nbsp;
  <a href="https://business-mjgtrading.myshopify.com/admin">Shopify Admin</a>
  &nbsp;·&nbsp; Generated automatically every Friday at 8 AM EST
</div>

</div>
</body></html>"""

    return html


def main():
    print("Starting MJG Trading weekly brand report...")

    now_utc = datetime.now(timezone.utc)
    now_est = now_utc.astimezone(EST)

    week_end = now_utc
    week_start = now_utc - timedelta(days=7)
    prev_week_start = now_utc - timedelta(days=14)

    client = ShopifyClient()

    print("Fetching collections + products...")
    collections = client.get_all_collections_with_products()
    print(f"  {len(collections)} collections")

    print("Fetching this week's orders...")
    orders_tw = client.get_orders_in_range(week_start, week_end)
    print(f"  {len(orders_tw)} orders this week")

    print("Fetching last week's orders...")
    orders_lw = client.get_orders_in_range(prev_week_start, week_start)
    print(f"  {len(orders_lw)} orders last week")

    html = build_email(collections, orders_tw, orders_lw, now_est)

    week_str = now_est.strftime("Week of %B %d, %Y")
    subject = f"MJG Trading — Weekly Brand Report · {week_str}"
    send_email(subject, html)
    print("Done.")


if __name__ == "__main__":
    main()
