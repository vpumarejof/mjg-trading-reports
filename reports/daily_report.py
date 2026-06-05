#!/usr/bin/env python3
"""
MJG Trading — Daily Inventory Report
Runs at 8 AM EST every day via GitHub Actions.
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shopify_client import ShopifyClient
from email_utils import send_email

STATE_FILE = Path(__file__).parent.parent / "state" / "inventory_state.json"
EST = timezone(timedelta(hours=-5))


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


def build_email(collections, products, state):
    now_utc = datetime.now(timezone.utc)
    yesterday = now_utc - timedelta(hours=24)
    prev_variants = state.get("daily_snapshot", {}).get("variants", {})

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

    out_of_stock_24h = []
    new_products = []
    total_active_skus = 0
    total_zero_skus = 0

    for product in products:
        if product["status"] != "ACTIVE":
            continue

        created_at = datetime.fromisoformat(product["createdAt"].replace("Z", "+00:00"))
        if created_at > yesterday:
            new_products.append({
                "title": product["title"],
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
                        "legacy_id": product["legacyResourceId"],
                        "sku": variant.get("sku") or "—",
                        "prev_qty": prev_qty,
                    })

    date_str = datetime.now(EST).strftime("%A, %B %d, %Y")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body{{font-family:Arial,sans-serif;color:#333;max-width:820px;margin:0 auto;padding:20px}}
  h1{{color:#1a1a2e;border-bottom:3px solid #e94560;padding-bottom:10px;margin-bottom:4px}}
  .meta{{color:#888;font-size:13px;margin-bottom:24px}}
  h2{{color:#16213e;margin:28px 0 10px;font-size:16px}}
  .section{{background:#f8f9fa;border-left:4px solid #ccc;padding:16px;margin:16px 0;border-radius:0 8px 8px 0}}
  .empty{{border-left-color:#dc3545}}
  .low{{border-left-color:#ffc107}}
  .oos{{border-left-color:#fd7e14}}
  .new{{border-left-color:#28a745}}
  .summary{{border-left-color:#0066cc}}
  table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:14px}}
  th{{background:#16213e;color:#fff;padding:8px 12px;text-align:left}}
  td{{padding:8px 12px;border-bottom:1px solid #dee2e6}}
  tr:last-child td{{border-bottom:none}}
  a{{color:#0066cc}}
  .badge{{background:#e94560;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:bold}}
  .stats{{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px}}
  .stat{{background:#fff;padding:14px 20px;border-radius:8px;flex:1;min-width:120px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,.08)}}
  .stat-num{{font-size:30px;font-weight:bold}}
  .stat-label{{font-size:11px;color:#888;margin-top:2px}}
  .ok{{color:#28a745;font-style:italic;font-size:14px}}
  .footer{{color:#aaa;font-size:11px;margin-top:32px;border-top:1px solid #eee;padding-top:12px}}
</style>
</head>
<body>
<h1>Daily Inventory Report — MJG Trading</h1>
<p class="meta">{date_str} &middot; 8:00 AM EST &middot; <a href="https://business-mjgtrading.myshopify.com/admin">Go to Admin</a></p>

<div class="section summary">
<h2 style="margin-top:0">Summary</h2>
<div class="stats">
  <div class="stat"><div class="stat-num" style="color:#28a745">{total_active_skus:,}</div><div class="stat-label">SKUs In Stock</div></div>
  <div class="stat"><div class="stat-num" style="color:#dc3545">{total_zero_skus:,}</div><div class="stat-label">SKUs Out of Stock</div></div>
  <div class="stat"><div class="stat-num" style="color:#dc3545">{len(empty_collections)}</div><div class="stat-label">Empty Collections</div></div>
  <div class="stat"><div class="stat-num" style="color:#ffc107">{len(low_stock_collections)}</div><div class="stat-label">Low Stock (&lt;3)</div></div>
  <div class="stat"><div class="stat-num" style="color:#fd7e14">{len(out_of_stock_24h)}</div><div class="stat-label">Out of Stock Today</div></div>
  <div class="stat"><div class="stat-num" style="color:#28a745">{len(new_products)}</div><div class="stat-label">New Today</div></div>
</div>
</div>

<div class="section empty">
<h2 style="margin-top:0">Collections Completely Empty — consider hiding them ({len(empty_collections)})</h2>"""

    if empty_collections:
        html += """<table><tr><th>Collection</th><th>Products</th><th>SKUs</th><th>Storefront</th><th>Admin</th></tr>"""
        for col in sorted(empty_collections, key=lambda x: x["title"]):
            html += f"""<tr>
  <td><strong>{col['title']}</strong></td>
  <td>{col['product_count']}</td>
  <td>{col['sku_count']}</td>
  <td><a href="{collection_url(col['handle'])}">View &rarr;</a></td>
  <td><a href="{admin_url(col['legacy_id'])}">Hide &rarr;</a></td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No completely empty collections.</p>'
    html += "</div>"

    html += f"""
<div class="section low">
<h2 style="margin-top:0">Collections with Less than 3 Units ({len(low_stock_collections)})</h2>"""
    if low_stock_collections:
        html += """<table><tr><th>Collection</th><th>Units</th><th>Products</th><th>Admin</th></tr>"""
        for col in sorted(low_stock_collections, key=lambda x: x["total_qty"]):
            html += f"""<tr>
  <td><strong>{col['title']}</strong></td>
  <td><span class="badge">{col['total_qty']}</span></td>
  <td>{col['product_count']}</td>
  <td><a href="{admin_url(col['legacy_id'])}">Admin &rarr;</a></td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No collections with low stock.</p>'
    html += "</div>"

    html += f"""
<div class="section oos">
<h2 style="margin-top:0">Products That Went Out of Stock in the Last 24h ({len(out_of_stock_24h)})</h2>"""
    if out_of_stock_24h:
        html += """<table><tr><th>Product</th><th>SKU</th><th>Previous Stock</th></tr>"""
        for item in out_of_stock_24h:
            html += f"""<tr>
  <td><a href="{product_admin_url(item['legacy_id'])}"><strong>{item['product_title']}</strong></a></td>
  <td>{item['sku']}</td>
  <td>{item['prev_qty']} units</td>
</tr>"""
        html += "</table>"
    else:
        html += '<p class="ok">No products went out of stock in the last 24h.</p>'
    html += "</div>"

    html += f"""
<div class="section new">
<h2 style="margin-top:0">New Products Added in the Last 24h ({len(new_products)})</h2>"""
    if new_products:
        html += """<table><tr><th>Product</th><th>SKUs</th><th>Admin</th></tr>"""
        for item in new_products:
            html += f"""<tr>
  <td><strong>{item['title']}</strong></td>
  <td>{item['sku_count']}</td>
  <td><a href="{product_admin_url(item['legacy_id'])}">View &rarr;</a></td>
</tr>"""
        html += "</table>"
    else:
        html += "<p class=\"ok\">No new products added in the last 24h.</p>"
    html += "</div>"

    html += """
<div class="footer">
  Automatically generated report &middot; MJG Trading &middot;
  <a href="https://business-mjgtrading.myshopify.com/admin">Shopify Admin</a>
</div>
</body></html>"""

    return html


def main():
    print("Starting MJG Trading daily inventory report...")

    client = ShopifyClient()
    state = load_state()

    print("Fetching collections...")
    collections = client.get_all_collections_with_products()
    print(f"  {len(collections)} collections found")

    print("Fetching products...")
    products = client.get_all_products()
    print(f"  {len(products)} products found")

    html = build_email(collections, products, state)

    now_est = datetime.now(EST)
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
