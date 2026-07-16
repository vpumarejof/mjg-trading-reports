#!/usr/bin/env python3
"""
MJG Trading — Daily Inventory Report
Runs at 8 AM EST every day via GitHub Actions.
"""

import base64
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from shopify_client import ShopifyClient, group_products_by_collection
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

CSS = """
  body{font-family:'Segoe UI',Arial,sans-serif;color:#1f2937;max-width:860px;margin:0 auto;padding:0;background:#f3f4f6}
  .wrapper{background:#fff;max-width:860px;margin:0 auto}
  .header{background:#ffffff;border-bottom:1px solid #e5e7eb;padding:24px 32px;display:flex;align-items:center;justify-content:space-between}
  .header-right{text-align:right;color:#6b7280;font-size:13px;line-height:1.6}
  .header-right strong{color:#0f172a;font-size:15px;display:block}
  .body{padding:28px 32px}
  h2{font-size:13px;font-weight:700;color:#0f172a;text-transform:uppercase;letter-spacing:.07em;margin:28px 0 10px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}
  .kpi-row{display:flex;gap:12px;margin-bottom:12px}
  .kpi{flex:1;background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:16px 18px}
  .kpi-num{font-size:24px;font-weight:700;color:#0f172a}
  .kpi-label{font-size:11px;color:#6b7280;margin-top:2px;text-transform:uppercase;letter-spacing:.04em}
  .kpi-sub{font-size:12px;color:#9ca3af;margin-top:4px}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:4px}
  th{background:#0f172a;color:#e2e8f0;padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
  td{padding:9px 12px;border-bottom:1px solid #f1f5f9;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:#f8fafc}
  a{color:#1e40af;text-decoration:none}
  a:hover{text-decoration:underline}
  .footer{background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 32px;font-size:11px;color:#9ca3af;text-align:center}
  .ok{color:#9ca3af;font-style:italic;font-size:13px}
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


def collection_url(handle):
    return f"https://business.mjgtrading.com/collections/{handle}"


def admin_url(legacy_id):
    return f"https://business-mjgtrading.myshopify.com/admin/collections/{legacy_id}"


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


def build_collections_summary(collections):
    empty_collections = []
    low_stock_collections = []

    for col in collections:
        active = [p for p in col["products"]["nodes"] if p["status"] == "ACTIVE"]
        if not active:
            continue

        all_variants = [v for p in active for v in p["variants"]["nodes"]]
        total_qty = sum(max(0, v["inventoryQuantity"] or 0) for v in all_variants)
        all_zero = all((v["inventoryQuantity"] or 0) <= 0 for v in all_variants)

        if all_zero:
            empty_collections.append({
                "title": col["title"],
                "handle": col["handle"],
                "legacy_id": col["legacyResourceId"],
                "sku_count": len(all_variants),
                "product_count": len(active),
            })
        elif 0 < total_qty < 3:
            low_stock_collections.append({
                "title": col["title"],
                "handle": col["handle"],
                "legacy_id": col["legacyResourceId"],
                "total_qty": total_qty,
                "product_count": len(active),
            })

    return empty_collections, low_stock_collections


def build_product_events(products, state):
    prev_variants = state.get("daily_snapshot", {}).get("variants", {})
    now_utc = datetime.now(timezone.utc)
    yesterday = now_utc - timedelta(hours=24)

    out_of_stock_24h = []
    new_products = []
    total_active_skus = 0
    total_zero_skus = 0

    for product in products:
        if product["status"] != "ACTIVE":
            continue

        vendor = normalize_vendor(product["vendor"])

        created_at = datetime.fromisoformat(product["createdAt"].replace("Z", "+00:00"))
        if created_at > yesterday:
            new_products.append({
                "title": product["title"],
                "vendor": vendor,
                "legacy_id": product["legacyResourceId"],
                "sku_count": len(product["variants"]["nodes"]),
            })

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
                        "legacy_id": product["legacyResourceId"],
                        "sku": variant.get("sku") or "—",
                        "prev_qty": prev_qty,
                    })

    out_of_stock_24h.sort(key=lambda x: (x["vendor"], x["product_title"]))
    new_products.sort(key=lambda x: (x["vendor"], x["title"]))

    return {
        "out_of_stock_24h": out_of_stock_24h,
        "new_products": new_products,
        "total_active_skus": total_active_skus,
        "total_zero_skus": total_zero_skus,
    }


def build_email(collections, products, sales, state):
    logo_b64 = load_logo_b64()
    logo_tag = (
        f'<img src="data:image/jpeg;base64,{logo_b64}" alt="MJG Trading" '
        f'style="width:80px;height:80px;object-fit:contain;display:block">'
        if logo_b64 else '<span style="font-size:20px;font-weight:700;color:#0f172a">MJG Trading</span>'
    )

    empty_collections, low_stock_collections = build_collections_summary(collections)
    events = build_product_events(products, state)
    out_of_stock_24h = events["out_of_stock_24h"]
    new_products = events["new_products"]

    date_str = datetime.now(NY_TZ).strftime("%A, %B %d, %Y")

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{CSS}</style></head>
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

<h2>Inventory Summary</h2>
<div class="kpi-row">
  <div class="kpi">
    <div class="kpi-num">{events['total_active_skus']:,}</div>
    <div class="kpi-label">SKUs In Stock</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{events['total_zero_skus']:,}</div>
    <div class="kpi-label">SKUs Out of Stock</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{len(empty_collections)}</div>
    <div class="kpi-label">Empty Collections</div>
  </div>
</div>
<div class="kpi-row">
  <div class="kpi">
    <div class="kpi-num">{len(low_stock_collections)}</div>
    <div class="kpi-label">Low Stock (&lt;3 units)</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{len(out_of_stock_24h)}</div>
    <div class="kpi-label">Went OOS Today</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{len(new_products)}</div>
    <div class="kpi-label">New Products Today</div>
  </div>
</div>

<h2>Yesterday's Sales</h2>
<div class="kpi-row">
  <div class="kpi">
    <div class="kpi-num">{fmt_money(sales['revenue'])}</div>
    <div class="kpi-label">Revenue</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{sales['orders']:,}</div>
    <div class="kpi-label">Orders</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{fmt_money(sales['aov'])}</div>
    <div class="kpi-label">Avg Order Value</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{sales['units']:,}</div>
    <div class="kpi-label">Units Sold</div>
  </div>
</div>
"""

    html += "<h2>Collections Completely Empty — consider hiding them</h2>"
    if empty_collections:
        html += "<table><tr><th>Collection</th><th>Products</th><th>SKUs</th><th>Storefront</th><th>Admin</th></tr>"
        for col in sorted(empty_collections, key=lambda x: x["title"]):
            html += f"""<tr>
  <td style="font-weight:600">{col['title']}</td>
  <td>{col['product_count']}</td>
  <td>{col['sku_count']}</td>
  <td><a href="{collection_url(col['handle'])}">View &rarr;</a></td>
  <td><a href="{admin_url(col['legacy_id'])}">Hide &rarr;</a></td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No completely empty collections.</p>'

    html += "<h2>Collections with Less than 3 Units</h2>"
    if low_stock_collections:
        html += "<table><tr><th>Collection</th><th>Units</th><th>Products</th><th>Admin</th></tr>"
        for col in sorted(low_stock_collections, key=lambda x: x["total_qty"]):
            html += f"""<tr>
  <td style="font-weight:600">{col['title']}</td>
  <td>{col['total_qty']}</td>
  <td>{col['product_count']}</td>
  <td><a href="{admin_url(col['legacy_id'])}">Admin &rarr;</a></td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No collections with low stock.</p>'

    html += "<h2>Products That Went Out of Stock in the Last 24h</h2>"
    if out_of_stock_24h:
        html += "<table><tr><th>Product</th><th>Brand</th><th>SKU</th><th>Previous Stock</th></tr>"
        for item in out_of_stock_24h:
            html += f"""<tr>
  <td><a href="{product_admin_url(item['legacy_id'])}" style="font-weight:600">{item['product_title']}</a></td>
  <td style="color:#6b7280;font-size:12px">{item['vendor']}</td>
  <td>{item['sku']}</td>
  <td>{item['prev_qty']} units</td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No products went out of stock in the last 24h.</p>'

    html += "<h2>New Products Added in the Last 24h</h2>"
    if new_products:
        html += "<table><tr><th>Product</th><th>Brand</th><th>SKUs</th><th>Admin</th></tr>"
        for item in new_products:
            html += f"""<tr>
  <td style="font-weight:600">{item['title']}</td>
  <td style="color:#6b7280;font-size:12px">{item['vendor']}</td>
  <td>{item['sku_count']}</td>
  <td><a href="{product_admin_url(item['legacy_id'])}">View &rarr;</a></td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No new products added in the last 24h.</p>'

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
    print("Starting MJG Trading daily inventory report...")

    client = ShopifyClient()
    state = load_state()

    now_local = datetime.now(timezone.utc).astimezone(NY_TZ)
    today_local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_local_midnight - timedelta(days=1)
    yesterday_end = today_local_midnight

    print("Fetching products...")
    products = client.get_all_products()
    print(f"  {len(products)} products found")

    print("Grouping products by collection...")
    collections = group_products_by_collection(products)
    print(f"  {len(collections)} collections found")

    print("Fetching yesterday's orders...")
    orders_yesterday = client.get_orders_in_range(yesterday_start, yesterday_end)
    print(f"  {len(orders_yesterday)} orders yesterday")
    sales = daily_sales_summary(orders_yesterday)

    html = build_email(collections, products, sales, state)

    now_est = datetime.now(NY_TZ)
    subject = f"MJG Trading Inventory Report — {now_est.strftime('%m/%d/%Y')}"
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
