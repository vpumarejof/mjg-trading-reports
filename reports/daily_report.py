#!/usr/bin/env python3
"""
MJG Trading — Daily Inventory Report
Runs at 8 AM EST every day via GitHub Actions.

Redesigned 2026-09-11 from Michael's feedback (Teams chat + "Catching Up"
call, 2026-09-10): bestsellers/trending grouped by BRAND instead of
collection, plus per-brand reorder and overstock alerts so the report tells
him what to act on instead of just describing inventory.
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from shopify_client import ShopifyClient
from email_utils import send_email

STATE_FILE = Path(__file__).parent.parent / "state" / "inventory_state.json"
NY_TZ = ZoneInfo("America/New_York")
LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo.jpg"

# Same alias table used by the weekly/monthly reports, so brand names shown
# here are consistent with those reports.
VENDOR_ALIASES = {
    "RAG & BONE":         "RAG AND BONE",
    "YVES SAINT LAURENT": "SAINT LAURENT",
    "JUICY":              "JUICY COUTURE",
}

# Confirmed with Valentina (2026-09-11): a brand counts as "fast-moving" /
# rotating if it sold at least this many units in the trailing 7 days.
# LOW_STOCK_THRESHOLD and OVERSTOCK_THRESHOLD are still placeholders —
# Michael gave a range ("150 to 200 pieces") for low stock but no exact
# number yet, and never gave one for overstock. Confirm both with him.
LOW_STOCK_THRESHOLD = 150
OVERSTOCK_THRESHOLD = 500
ROTATION_MIN_UNITS_7D = 5

CSS = """
  body{font-family:'Segoe UI',Arial,sans-serif;color:#1f2937;max-width:860px;margin:0 auto;padding:0;background:#f3f4f6;-webkit-text-size-adjust:100%}
  .wrapper{background:#fff;max-width:860px;margin:0 auto}
  .header{background:#ffffff;border-bottom:1px solid #e5e7eb;padding:24px 32px;display:flex;align-items:center;justify-content:space-between}
  .header-right{text-align:right;color:#6b7280;font-size:13px;line-height:1.6}
  .header-right strong{color:#0f172a;font-size:15px;display:block}
  .logo-img{width:80px;height:80px;object-fit:contain;display:block}
  .body{padding:28px 32px}
  h2{font-size:13px;font-weight:700;color:#0f172a;text-transform:uppercase;letter-spacing:.07em;margin:28px 0 10px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}
  .kpi-row{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:12px}
  .kpi{flex:1;min-width:120px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:16px 18px}
  .kpi-num{font-size:24px;font-weight:700;color:#0f172a}
  .kpi-label{font-size:11px;color:#6b7280;margin-top:2px;text-transform:uppercase;letter-spacing:.04em}
  .kpi-sub{font-size:12px;color:#6b7280;margin-top:6px;line-height:1.5}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:4px}
  th{background:#0f172a;color:#e2e8f0;padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
  td{padding:9px 12px;border-bottom:1px solid #f1f5f9;vertical-align:top;overflow-wrap:anywhere}
  tr:last-child td{border-bottom:none}
  a{color:#1e40af;text-decoration:none}
  .footer{background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 32px;font-size:11px;color:#9ca3af;text-align:center}
  .ok{color:#9ca3af;font-style:italic;font-size:13px}
  .tot{font-weight:700;color:#0f172a}
  .split{font-size:11px;color:#6b7280;line-height:1.45;margin-top:2px}
  .lbl{display:inline-block;min-width:26px;font-weight:600;color:#9ca3af}
  .brand{font-weight:600}
  .sub{font-size:11px;color:#6b7280;margin-top:2px}
  .tag-reorder{display:inline-block;margin-top:4px;background:#fef2f2;color:#b91c1c;font-weight:700;font-size:10px;padding:2px 6px;border-radius:4px}
  .tag-overstock{display:inline-block;margin-top:4px;background:#fffbeb;color:#b45309;font-weight:700;font-size:10px;padding:2px 6px;border-radius:4px}

  @media only screen and (max-width:600px){
    .body{padding:16px 12px}
    .header{padding:12px 16px}
    .logo-img{width:52px;height:52px}
    h2{font-size:11px;margin:20px 0 8px;padding-bottom:4px;letter-spacing:.04em}
    .kpi-row{display:block;margin-bottom:8px}
    .kpi{display:inline-block;width:47%;min-width:0;margin:0 1% 6px 0;padding:9px 10px;box-sizing:border-box;vertical-align:top}
    .kpi-num{font-size:17px}
    .kpi-label{font-size:9px}
    .kpi-sub{font-size:10.5px}
    .footer{padding:12px 14px;font-size:10px}
    table{table-layout:fixed;font-size:11px}
    th{padding:6px 4px;font-size:8.5px;letter-spacing:.02em}
    td{padding:7px 4px;font-size:11px}
    .split{font-size:9.5px}
    .lbl{min-width:22px}
    .sub{font-size:9.5px}
  }
"""


def normalize_vendor(raw):
    v = (raw or "").strip().upper()
    return VENDOR_ALIASES.get(v, v)


def load_logo_b64():
    if LOGO_PATH.exists():
        return base64.b64encode(LOGO_PATH.read_bytes()).decode()
    return None


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def product_admin_url(legacy_id):
    return f"https://business-mjgtrading.myshopify.com/admin/products/{legacy_id}"


def fmt_money(v):
    return f"${v:,.2f}"


def daily_sales_summary(orders):
    revenue = sum(float(o["currentTotalPriceSet"]["shopMoney"]["amount"]) for o in orders)
    count = len(orders)
    units = sum(item["currentQuantity"] for o in orders for item in o["lineItems"]["nodes"])
    return {
        "revenue": revenue,
        "orders": count,
        "aov": revenue / count if count else 0,
        "units": units,
    }


def build_product_events(products, state):
    prev_variants = state.get("daily_snapshot", {}).get("variants", {})
    now_utc = datetime.now(timezone.utc)
    yesterday = now_utc - timedelta(hours=24)

    out_of_stock_24h = []
    total_active_skus = 0
    total_zero_skus = 0

    for product in products:
        if product["status"] != "ACTIVE":
            continue

        vendor = normalize_vendor(product["vendor"])

        for variant in product["variants"]["nodes"]:
            qty = max(0, variant["inventoryQuantity"] or 0)
            if qty > 0:
                total_active_skus += 1
            else:
                total_zero_skus += 1
                prev_qty = prev_variants.get(variant["id"], {}).get("qty")
                if prev_qty is not None and prev_qty > 0:
                    out_of_stock_24h.append({
                        "product_title": product["title"],
                        "vendor": vendor,
                        "category": product_category(product),
                        "legacy_id": product["legacyResourceId"],
                        "sku": variant.get("sku") or "—",
                        "prev_qty": prev_qty,
                    })

    out_of_stock_24h.sort(key=lambda x: (x["vendor"], x["product_title"]))

    return {
        "out_of_stock_24h": out_of_stock_24h,
        "total_active_skus": total_active_skus,
        "total_zero_skus": total_zero_skus,
    }


def product_vendor_map(products):
    return {p["id"]: normalize_vendor(p["vendor"]) for p in products}


def product_category_map(products):
    return {p["id"]: product_category(p) for p in products}


def product_category(product):
    """SUN or RX (optical), from productType, falling back to tags.

    Michael asked (2026-09-24) for stock to be split into SUN and Optical
    instead of one merged number. Readers count as RX. Anything that can't
    be classified (e.g. the DROPSHIP placeholder) is "OTHER".
    """
    ptype = (product.get("productType") or "").lower()
    if "sunglass" in ptype:
        return "SUN"
    if "eyeglass" in ptype or "reader" in ptype:
        return "RX"
    tags = {t.lower() for t in product.get("tags") or []}
    if "sunglasses" in tags:
        return "SUN"
    if "eyeglasses" in tags:
        return "RX"
    return "OTHER"


def stock_by_brand(products):
    stock = {}
    for p in products:
        if p["status"] != "ACTIVE":
            continue
        vendor = normalize_vendor(p["vendor"])
        total = sum(max(0, v["inventoryQuantity"] or 0) for v in p["variants"]["nodes"])
        row = stock.setdefault(vendor, empty_split())
        row[product_category(p)] += total
    return stock


CATEGORIES = ("SUN", "RX", "OTHER")


def empty_split():
    return {c: 0 for c in CATEGORIES}


def split_cell(split, fmt=lambda v: f"{v:,}"):
    """Total on top, SUN / RX (and OTHER only when non-zero) stacked below.

    Stacking instead of one "X SUN / Y RX" line keeps every column narrow
    enough to fit a phone screen without horizontal scrolling.
    """
    total = sum(split.values())
    lines = [f'<span class="lbl">SUN</span>{fmt(split["SUN"])}',
             f'<span class="lbl">RX</span>{fmt(split["RX"])}']
    if split["OTHER"]:
        lines.append(f'<span class="lbl">OTH</span>{fmt(split["OTHER"])}')
    return f'<div class="tot">{fmt(total)}</div><div class="split">{"<br>".join(lines)}</div>'


def fmt_money0(v):
    return f"${v:,.0f}"


def fmt_trend(v):
    return f"+{v:,}" if v > 0 else f"{v:,}"


def sales_by_brand(orders, vendor_map, category_map):
    out = {}
    for o in orders:
        for li in o["lineItems"]["nodes"]:
            product = li.get("product")
            if not product:
                continue
            vendor = vendor_map.get(product["id"])
            if not vendor:
                continue
            qty = li.get("currentQuantity") or 0
            if qty <= 0:
                continue
            price_set = li.get("discountedUnitPriceSet") or {}
            price = (price_set.get("shopMoney") or {}).get("amount")
            revenue = qty * float(price) if price else 0.0
            cat = category_map.get(product["id"], "OTHER")
            row = out.setdefault(vendor, {"units": empty_split(), "revenue": empty_split()})
            row["units"][cat] += qty
            row["revenue"][cat] += revenue
    return out


def build_brand_rows(stock, this_week, prior_week, month):
    brands = set(stock) | set(this_week) | set(prior_week) | set(month)
    blank = {"units": empty_split(), "revenue": empty_split()}
    rows = []
    for brand in brands:
        cur = this_week.get(brand, blank)
        prev = prior_week.get(brand, blank)
        mo = month.get(brand, blank)
        split = stock.get(brand, empty_split())
        rows.append({
            "brand": brand,
            "stock": sum(split.values()),
            "stock_split": split,
            "units_7d": sum(cur["units"].values()),
            "units_7d_split": cur["units"],
            "revenue_7d_split": cur["revenue"],
            "trend": sum(cur["units"].values()) - sum(prev["units"].values()),
            "trend_split": {c: cur["units"][c] - prev["units"][c] for c in CATEGORIES},
            "units_30d": sum(mo["units"].values()),
            "units_30d_split": mo["units"],
            "revenue_30d_split": mo["revenue"],
        })
    return rows


def trend_badge(trend, split):
    if trend > 0:
        head = f'<span style="color:#059669;font-weight:700">▲ +{trend}</span>'
    elif trend < 0:
        head = f'<span style="color:#dc2626;font-weight:700">▼ {trend}</span>'
    else:
        head = '<span style="color:#9ca3af;font-weight:700">— 0</span>'
    lines = [f'<span class="lbl">SUN</span>{fmt_trend(split["SUN"])}',
             f'<span class="lbl">RX</span>{fmt_trend(split["RX"])}']
    if split["OTHER"]:
        lines.append(f'<span class="lbl">OTH</span>{fmt_trend(split["OTHER"])}')
    return f'<div>{head}</div><div class="split">{"<br>".join(lines)}</div>'


def yesterday_split(orders, category_map):
    units, revenue = empty_split(), empty_split()
    for o in orders:
        for li in o["lineItems"]["nodes"]:
            qty = li.get("currentQuantity") or 0
            if qty <= 0:
                continue
            product = li.get("product")
            cat = category_map.get(product["id"], "OTHER") if product else "OTHER"
            price = ((li.get("discountedUnitPriceSet") or {}).get("shopMoney") or {}).get("amount")
            units[cat] += qty
            revenue[cat] += qty * float(price) if price else 0.0
    return units, revenue


def kpi_sub(split, fmt):
    text = f'SUN {fmt(split["SUN"])} · RX {fmt(split["RX"])}'
    if split["OTHER"]:
        text += f' · Other {fmt(split["OTHER"])}'
    return f'<div class="kpi-sub">{text}</div>'


def build_email(products, sales, brand_rows, state, sales_units_split, sales_revenue_split):
    logo_b64 = load_logo_b64()
    logo_tag = (
        f'<img src="data:image/jpeg;base64,{logo_b64}" alt="MJG Trading" class="logo-img">'
        if logo_b64 else '<span style="font-size:20px;font-weight:700;color:#0f172a">MJG Trading</span>'
    )

    events = build_product_events(products, state)
    out_of_stock_24h = events["out_of_stock_24h"]

    date_str = datetime.now(NY_TZ).strftime("%A, %B %d, %Y")

    bestsellers_week = sorted(brand_rows, key=lambda r: r["units_7d"], reverse=True)[:12]
    bestsellers_month = sorted(brand_rows, key=lambda r: r["units_30d"], reverse=True)[:12]
    reorder_alerts = sorted(
        [r for r in brand_rows if r["stock"] < LOW_STOCK_THRESHOLD and r["units_7d"] >= ROTATION_MIN_UNITS_7D],
        key=lambda r: r["stock"],
    )
    overstock_alerts = sorted(
        [r for r in brand_rows if r["stock"] > OVERSTOCK_THRESHOLD and r["units_7d"] < ROTATION_MIN_UNITS_7D],
        key=lambda r: r["stock"],
        reverse=True,
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>{CSS}</style></head>
<body>
<div class="wrapper">

<div class="header">
  <div>{logo_tag}</div>
  <div class="header-right">
    <strong>Daily Inventory Report</strong>
    {date_str}
  </div>
</div>

<div class="body">

<h2>Yesterday's Sales</h2>
<div class="kpi-row">
  <div class="kpi">
    <div class="kpi-num">{fmt_money(sales['revenue'])}</div>
    <div class="kpi-label">Revenue</div>
    {kpi_sub(sales_revenue_split, fmt_money0)}
  </div>
  <div class="kpi">
    <div class="kpi-num">{sales['units']:,}</div>
    <div class="kpi-label">Units Sold</div>
    {kpi_sub(sales_units_split, lambda v: f"{v:,}")}
  </div>
  <div class="kpi">
    <div class="kpi-num">{sales['orders']:,}</div>
    <div class="kpi-label">Orders</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{fmt_money(sales['aov'])}</div>
    <div class="kpi-label">Avg Order Value</div>
  </div>
</div>
<p style="font-size:11px;color:#9ca3af;margin:0 0 4px">Revenue split is product sales only (before shipping and taxes), so it can differ slightly from the total.</p>
"""

    html += "<h2>Brand Performance — Last 7 Days</h2>"
    if bestsellers_week:
        html += ('<table><colgroup><col style="width:24%"><col style="width:17%"><col style="width:21%">'
                 '<col style="width:21%"><col style="width:17%"></colgroup>'
                 '<tr><th>Brand</th><th>Units 7d</th><th>Revenue 7d</th><th>Stock</th><th>vs Prior 7d</th></tr>')
        for r in bestsellers_week:
            html += f"""<tr>
  <td class="brand">{r['brand']}</td>
  <td>{split_cell(r['units_7d_split'])}</td>
  <td>{split_cell(r['revenue_7d_split'], fmt_money0)}</td>
  <td>{split_cell(r['stock_split'])}</td>
  <td>{trend_badge(r['trend'], r['trend_split'])}</td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No brand sales in the last 7 days.</p>'

    html += "<h2>Brand Performance — Last 30 Days</h2>"
    if bestsellers_month:
        html += ('<table><colgroup><col style="width:28%"><col style="width:22%"><col style="width:26%">'
                 '<col style="width:24%"></colgroup>'
                 '<tr><th>Brand</th><th>Units 30d</th><th>Revenue 30d</th><th>Stock</th></tr>')
        for r in bestsellers_month:
            html += f"""<tr>
  <td class="brand">{r['brand']}</td>
  <td>{split_cell(r['units_30d_split'])}</td>
  <td>{split_cell(r['revenue_30d_split'], fmt_money0)}</td>
  <td>{split_cell(r['stock_split'])}</td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No brand sales in the last 30 days.</p>'

    html += f"<h2>Reorder Alerts — Fast-Moving Brands Below {LOW_STOCK_THRESHOLD} Units</h2>"
    if reorder_alerts:
        html += ('<table><colgroup><col style="width:31%"><col style="width:23%"><col style="width:23%">'
                 '<col style="width:23%"></colgroup>'
                 '<tr><th>Brand</th><th>Stock</th><th>Units 7d</th><th>Units 30d</th></tr>')
        for r in reorder_alerts:
            html += f"""<tr>
  <td class="brand">{r['brand']}<br><span class="tag-reorder">REORDER</span></td>
  <td>{split_cell(r['stock_split'])}</td>
  <td>{split_cell(r['units_7d_split'])}</td>
  <td>{split_cell(r['units_30d_split'])}</td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No fast-moving brands are currently below the low-stock threshold.</p>'

    html += f"<h2>Overstock Watch — Slow-Moving Brands Above {OVERSTOCK_THRESHOLD} Units</h2>"
    if overstock_alerts:
        html += ('<table><colgroup><col style="width:31%"><col style="width:23%"><col style="width:23%">'
                 '<col style="width:23%"></colgroup>'
                 '<tr><th>Brand</th><th>Stock</th><th>Units 7d</th><th>Units 30d</th></tr>')
        for r in overstock_alerts:
            html += f"""<tr>
  <td class="brand">{r['brand']}<br><span class="tag-overstock">HOLD OFF</span></td>
  <td>{split_cell(r['stock_split'])}</td>
  <td>{split_cell(r['units_7d_split'])}</td>
  <td>{split_cell(r['units_30d_split'])}</td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No brands currently flagged as overstocked and slow-moving.</p>'

    html += "<h2>Products That Went Out of Stock in the Last 24h</h2>"
    if out_of_stock_24h:
        oos_split = empty_split()
        for item in out_of_stock_24h:
            oos_split[item["category"]] += 1
        summary = f'{len(out_of_stock_24h)} SKUs · SUN {oos_split["SUN"]} · RX {oos_split["RX"]}'
        if oos_split["OTHER"]:
            summary += f' · Other {oos_split["OTHER"]}'
        html += f'<p style="font-size:12px;color:#6b7280;margin:0 0 8px">{summary}</p>'
        html += ('<table><colgroup><col style="width:46%"><col style="width:14%"><col style="width:22%">'
                 '<col style="width:18%"></colgroup>'
                 '<tr><th>Product</th><th>Type</th><th>SKU</th><th>Prev Stock</th></tr>')
        for item in out_of_stock_24h:
            html += f"""<tr>
  <td><a href="{product_admin_url(item['legacy_id'])}" style="font-weight:600">{item['product_title']}</a><div class="sub">{item['vendor']}</div></td>
  <td>{item['category']}</td>
  <td>{item['sku']}</td>
  <td>{item['prev_qty']}</td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No products went out of stock in the last 24h.</p>'

    html += f"""
</div>
<div class="footer">
  MJG Trading &nbsp;·&nbsp; Daily Inventory Report &nbsp;·&nbsp;
  <a href="https://business-mjgtrading.myshopify.com/admin">Shopify Admin</a>
  &nbsp;·&nbsp; Generated automatically every day at 8 AM EST
</div>

</div>
</body></html>"""

    return html


def main():
    dry_run = "--dry-run" in sys.argv

    print("Starting MJG Trading daily inventory report...")

    client = ShopifyClient()
    state = load_state()

    now_local = datetime.now(timezone.utc).astimezone(NY_TZ)
    today_local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_local_midnight - timedelta(days=1)
    yesterday_end = today_local_midnight
    week_start = today_local_midnight - timedelta(days=7)
    prior_week_start = today_local_midnight - timedelta(days=14)
    month_start = today_local_midnight - timedelta(days=30)

    print("Fetching products...")
    products = client.get_all_products()
    print(f"  {len(products)} products found")

    print("Fetching yesterday's orders...")
    orders_yesterday = client.get_orders_in_range(yesterday_start, yesterday_end)
    print(f"  {len(orders_yesterday)} orders yesterday")
    sales = daily_sales_summary(orders_yesterday)

    print("Fetching last 30 days of orders (for brand trending + monthly bestsellers)...")
    orders_30d = client.get_orders_in_range(month_start, today_local_midnight)
    orders_this_week = [o for o in orders_30d if o["createdAt"] >= week_start.isoformat()]
    orders_prior_week = [
        o for o in orders_30d
        if prior_week_start.isoformat() <= o["createdAt"] < week_start.isoformat()
    ]

    vendor_map = product_vendor_map(products)
    category_map = product_category_map(products)
    stock = stock_by_brand(products)
    this_week_sales = sales_by_brand(orders_this_week, vendor_map, category_map)
    prior_week_sales = sales_by_brand(orders_prior_week, vendor_map, category_map)
    month_sales = sales_by_brand(orders_30d, vendor_map, category_map)
    brand_rows = build_brand_rows(stock, this_week_sales, prior_week_sales, month_sales)
    units_split, revenue_split = yesterday_split(orders_yesterday, category_map)

    html = build_email(products, sales, brand_rows, state, units_split, revenue_split)

    now_est = datetime.now(NY_TZ)
    subject = f"MJG Trading Inventory Report — {now_est.strftime('%m/%d/%Y')}"

    if dry_run:
        out_path = Path(__file__).parent / "daily_report_preview.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"Dry run — HTML written to {out_path}, no email sent, state not updated.")
        return

    # TEST_RECIPIENT (set via the workflow_dispatch "test_recipient" input)
    # sends only to that address and leaves the snapshot untouched, so a test
    # run never reaches Michael/Adam or shifts tomorrow's out-of-stock diff.
    test_recipient = os.environ.get("TEST_RECIPIENT", "").strip()
    if test_recipient:
        os.environ["REPORT_EMAIL"] = test_recipient
        send_email(f"[TEST] {subject}", html)
        print("Test run — state not updated.")
        return

    send_email(subject, html)

    # Update daily snapshot for tomorrow's "out of stock 24h" detection
    variant_snapshot = {}
    for product in products:
        if product["status"] == "ACTIVE":
            for variant in product["variants"]["nodes"]:
                variant_snapshot[variant["id"]] = {
                    "qty": max(0, variant["inventoryQuantity"] or 0)
                }

    state["daily_snapshot"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "variants": variant_snapshot,
    }
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
