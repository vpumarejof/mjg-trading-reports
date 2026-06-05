#!/usr/bin/env python3
"""
MJG Trading — Collection Empty Alert
Runs every 15 minutes via GitHub Actions.
Sends an immediate email when a collection's total inventory hits zero.
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


def build_alert_email(alerts):
    count = len(alerts)
    now_str = datetime.now(EST).strftime("%d/%m/%Y %I:%M %p EST")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body{{font-family:Arial,sans-serif;color:#333;max-width:700px;margin:0 auto;padding:20px}}
  .header{{background:#dc3545;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0;margin-bottom:0}}
  .header h1{{margin:0;font-size:20px}}
  .header p{{margin:6px 0 0;opacity:.85;font-size:13px}}
  .card{{border:1px solid #dee2e6;border-left:5px solid #dc3545;padding:20px;margin:20px 0;border-radius:0 8px 8px 0}}
  .col-title{{font-size:20px;font-weight:bold;color:#1a1a2e;margin:0 0 4px}}
  .col-url{{color:#0066cc;font-size:13px;margin:0 0 16px}}
  .stats{{display:flex;gap:16px;margin:16px 0}}
  .stat{{flex:1;background:#f8f9fa;padding:12px;border-radius:6px;text-align:center}}
  .stat-num{{font-size:24px;font-weight:bold;color:#dc3545}}
  .stat-label{{font-size:11px;color:#888;margin-top:2px}}
  .product-box{{background:#fff3cd;border:1px solid #ffc107;padding:10px 14px;border-radius:6px;margin:8px 0;font-size:14px}}
  .btn{{display:inline-block;padding:10px 20px;border-radius:6px;text-decoration:none;margin:6px 4px 0 0;font-size:14px;font-weight:bold}}
  .btn-red{{background:#dc3545;color:#fff}}
  .btn-dark{{background:#1a1a2e;color:#fff}}
  .footer{{color:#aaa;font-size:11px;margin-top:24px;text-align:center}}
</style>
</head>
<body>
<div class="header">
  <h1>ALERT: {count} Empty Collection{"s" if count > 1 else ""}</h1>
  <p>MJG Trading &middot; {now_str}</p>
</div>"""

    for alert in alerts:
        col = alert["collection"]
        sold = alert["sold_last_unit"]

        html += f"""
<div class="card">
  <p class="col-title">{col['title']}</p>
  <p class="col-url">{collection_url(col['handle'])}</p>

  <div class="stats">
    <div class="stat"><div class="stat-num">{col['product_count']}</div><div class="stat-label">Productos</div></div>
    <div class="stat"><div class="stat-num">{col['sku_count']}</div><div class="stat-label">SKUs</div></div>
    <div class="stat"><div class="stat-num">0</div><div class="stat-label">Unidades ahora</div></div>
  </div>"""

        if sold:
            html += "<p style='font-weight:bold;margin:14px 0 6px'>Product that sold the last unit:</p>"
            for item in sold:
                html += f"""<div class="product-box">
  <strong>{item['product_title']}</strong><br>
  SKU: {item['sku'] or '—'} &middot; Had {item['prev_qty']} unit{'s' if item['prev_qty'] != 1 else ''}
</div>"""

        html += f"""
  <div style="margin-top:16px">
    <a class="btn btn-red" href="{admin_url(col['legacy_id'])}">Hide collection &rarr;</a>
    <a class="btn btn-dark" href="{collection_url(col['handle'])}">View in store &rarr;</a>
  </div>
</div>"""

    html += """
<div class="footer">
  Automatic alert &middot; MJG Trading &middot;
  <a href="https://business-mjgtrading.myshopify.com/admin">Admin Shopify</a>
</div>
</body></html>"""

    return html


def main():
    print("Checking collection inventory levels...")

    client = ShopifyClient()
    state = load_state()
    prev_collections = state.get("current", {}).get("collections", {})
    prev_variants = state.get("current", {}).get("variants", {})

    print("Fetching current collection inventory...")
    collections = client.get_all_collections_with_products()

    current_variants = {}
    current_collections = {}
    alerts = []

    for col in collections:
        active = [p for p in col["products"]["nodes"] if p["status"] == "ACTIVE"]
        if not active:
            continue

        col_id = col["id"]
        col_total_qty = 0
        sku_count = 0

        for product in active:
            for variant in product["variants"]["nodes"]:
                vid = variant["id"]
                qty = max(0, variant["inventoryQuantity"] or 0)
                col_total_qty += qty
                sku_count += 1
                if vid not in current_variants:
                    current_variants[vid] = {
                        "qty": qty,
                        "product_title": product["title"],
                        "sku": variant.get("sku") or "",
                    }

        current_collections[col_id] = {
            "title": col["title"],
            "handle": col["handle"],
            "legacy_id": col["legacyResourceId"],
            "total_qty": col_total_qty,
            "sku_count": sku_count,
            "product_count": len(active),
        }

        # Detect: was positive before, now zero
        prev_col = prev_collections.get(col_id, {})
        prev_total = prev_col.get("total_qty")
        if prev_total is not None and prev_total > 0 and col_total_qty == 0:
            sold_last_unit = []
            for product in active:
                for variant in product["variants"]["nodes"]:
                    vid = variant["id"]
                    prev_qty = prev_variants.get(vid, {}).get("qty", 0)
                    curr_qty = max(0, variant["inventoryQuantity"] or 0)
                    if prev_qty > 0 and curr_qty == 0:
                        sold_last_unit.append({
                            "product_title": product["title"],
                            "sku": variant.get("sku") or "",
                            "prev_qty": prev_qty,
                        })

            alerts.append({
                "collection": current_collections[col_id],
                "sold_last_unit": sold_last_unit,
            })
            print(f"  ALERT: '{col['title']}' just went to zero!")

    if alerts:
        html = build_alert_email(alerts)
        names = ", ".join(a["collection"]["title"] for a in alerts)
        send_email(f"ALERT Empty Collection: {names}", html)
    else:
        print("  No newly empty collections.")

    state["current"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "collections": current_collections,
        "variants": current_variants,
    }
    save_state(state)
    print(f"State updated — {len(current_collections)} collections tracked.")


if __name__ == "__main__":
    main()
