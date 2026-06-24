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

# Vendors that are internal categories, not brands
VENDOR_EXCLUDE = {"OPEN BOX", "DROPSHIP", "SIGNATURE", "PRIMARY"}

# Merge these into a canonical name (keys are normalized to uppercase first)
VENDOR_ALIASES = {
    "RAG & BONE":           "RAG AND BONE",
    "YVES SAINT LAURENT":   "SAINT LAURENT",
    "JUICY":                "JUICY COUTURE",
}

STATUS_ORDER = ["REORDER NOW", "REORDER SOON", "SOLD OUT", "MONITOR", "ADEQUATE", "OK"]
STATUS_STYLE = {
    "REORDER NOW":  "font-weight:700;color:#b91c1c",
    "REORDER SOON": "font-weight:600;color:#92400e",
    "SOLD OUT":     "font-weight:600;color:#374151",
    "MONITOR":      "color:#9ca3af;font-style:italic",
    "ADEQUATE":     "color:#166534",
    "OK":           "color:#166534",
}


def normalize_vendor(raw):
    v = (raw or "").strip().upper()
    return VENDOR_ALIASES.get(v, v)


def load_logo_b64():
    if LOGO_PATH.exists():
        return base64.b64encode(LOGO_PATH.read_bytes()).decode()
    return None


def thumb(url, width=120):
    if not url:
        return None
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}width={width}"


def build_brand_data(products, orders_tw, orders_lw):
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

    units_tw, rev_tw = tally(orders_tw)
    units_lw, rev_lw = tally(orders_lw)

    # Group active products by normalized vendor
    brand_products = {}
    for p in products:
        if p["status"] != "ACTIVE":
            continue
        vendor = normalize_vendor(p["vendor"])
        if vendor in VENDOR_EXCLUDE or not vendor:
            continue
        brand_products.setdefault(vendor, []).append(p)

    brands = []
    for vendor, prods in sorted(brand_products.items()):
        stock = sum(
            max(0, v["inventoryQuantity"] or 0)
            for p in prods for v in p["variants"]["nodes"]
        )
        u_tw = sum(units_tw.get(p["legacyResourceId"], 0) for p in prods)
        u_lw = sum(units_lw.get(p["legacyResourceId"], 0) for p in prods)
        r_tw = sum(rev_tw.get(p["legacyResourceId"], 0.0) for p in prods)
        r_lw = sum(rev_lw.get(p["legacyResourceId"], 0.0) for p in prods)

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
                status = "OK"

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
            "title": vendor,
            "product_count": len(prods),
            "stock": stock,
            "u_tw": u_tw, "u_lw": u_lw,
            "r_tw": r_tw, "r_lw": r_lw,
            "days_of_stock": days_of_stock,
            "trend": trend,
            "status": status,
        })

    return sorted(brands, key=lambda b: (STATUS_ORDER.index(b["status"]), -b["u_tw"]))


def build_top_products(products, orders_tw):
    # Build product lookup for vendor + image
    product_meta = {
        p["legacyResourceId"]: {
            "title": p["title"],
            "vendor": normalize_vendor(p["vendor"]),
            "image": thumb(p["featuredImage"]["url"]) if p.get("featuredImage") else None,
        }
        for p in products
        if p["status"] == "ACTIVE" and normalize_vendor(p["vendor"]) not in VENDOR_EXCLUDE
    }

    products_sold = {}
    for order in orders_tw:
        for item in order["lineItems"]["nodes"]:
            if not item.get("product"):
                continue
            pid = item["product"]["legacyResourceId"]
            if pid not in product_meta:
                continue
            if pid not in products_sold:
                products_sold[pid] = {
                    **product_meta[pid],
                    "legacy_id": pid,
                    "units": 0,
                    "revenue": 0.0,
                }
            products_sold[pid]["units"] += item["quantity"]
            products_sold[pid]["revenue"] += (
                float(item["originalUnitPriceSet"]["shopMoney"]["amount"]) * item["quantity"]
            )

    return sorted(products_sold.values(), key=lambda x: x["units"], reverse=True)[:10]


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


def customer_breakdown(orders_tw):
    new_c = sum(1 for o in orders_tw if o.get("customer") and int(o["customer"]["numberOfOrders"]) == 1)
    returning = sum(1 for o in orders_tw if o.get("customer") and int(o["customer"]["numberOfOrders"]) > 1)
    guest = sum(1 for o in orders_tw if not o.get("customer"))
    total = len(orders_tw) or 1
    return {"new": new_c, "returning": returning, "guest": guest,
            "new_pct": round(new_c / total * 100), "returning_pct": round(returning / total * 100)}


def abandoned_summary(abandoned):
    total_value = sum(float(c["totalLineItemsPriceSet"]["shopMoney"]["amount"]) for c in abandoned)
    # Count product frequency in abandoned carts
    products_in_carts = {}
    for c in abandoned:
        for item in c["lineItems"]["nodes"]:
            if not item.get("variant") or not item["variant"].get("product"):
                continue
            pid = item["variant"]["product"]["legacyResourceId"]
            title = item["variant"]["product"]["title"]
            vendor = normalize_vendor(item["variant"]["product"].get("vendor", ""))
            products_in_carts.setdefault(pid, {"title": title, "vendor": vendor, "count": 0, "qty": 0})
            products_in_carts[pid]["count"] += 1
            products_in_carts[pid]["qty"] += item["quantity"]
    top = sorted(products_in_carts.values(), key=lambda x: -x["count"])[:5]
    return {"count": len(abandoned), "value": total_value, "top_products": top}


def dead_stock_brands(products, orders_tw, orders_lw):
    # Products sold in either week
    sold_pids = set()
    for o in orders_tw + orders_lw:
        for item in o["lineItems"]["nodes"]:
            if item.get("product"):
                sold_pids.add(item["product"]["legacyResourceId"])

    # Group unsold active products by brand
    brand_dead = {}
    for p in products:
        if p["status"] != "ACTIVE":
            continue
        if p["legacyResourceId"] in sold_pids:
            continue
        vendor = normalize_vendor(p["vendor"])
        if vendor in VENDOR_EXCLUDE or not vendor:
            continue
        stock = sum(max(0, v["inventoryQuantity"] or 0) for v in p["variants"]["nodes"])
        if stock == 0:
            continue
        price = float(p["priceRangeV2"]["minVariantPrice"]["amount"]) if p.get("priceRangeV2") else 0
        retail_value = stock * price
        brand_dead.setdefault(vendor, {"stock": 0, "value": 0.0})
        brand_dead[vendor]["stock"] += stock
        brand_dead[vendor]["value"] += retail_value

    result = [{"brand": k, **v} for k, v in brand_dead.items() if v["stock"] > 0]
    return sorted(result, key=lambda x: -x["value"])[:10]


def build_improvement_section(products, orders_tw, orders_lw, abandoned, brands):
    cust = customer_breakdown(orders_tw)
    ab = abandoned_summary(abandoned)
    dead = dead_stock_brands(products, orders_tw, orders_lw)

    # Auto-generate recommendations
    recs = []
    if ab["count"] > 0:
        recs.append(f"<strong>{ab['count']} abandoned checkouts</strong> left {fmt_money(ab['value'])} on the table — activate Shopify's abandoned checkout email to recover it.")
    if cust["new_pct"] < 10 and len(orders_tw) > 5:
        recs.append(f"Only <strong>{cust['new_pct']}% new customers</strong> this week — consider paid acquisition, referrals, or influencer outreach to grow your base.")
    elif cust["returning_pct"] >= 80:
        recs.append(f"<strong>{cust['returning_pct']}% returning customers</strong> — strong loyalty. Leverage this with a VIP or early-access program.")
    reorder_now = [b for b in brands if b["status"] == "REORDER NOW"]
    for b in reorder_now[:3]:
        recs.append(f"<strong>{b['title']}</strong> has only {b['days_of_stock']} days of stock left — place a reorder before the weekend.")
    growing = [b for b in brands if b["u_lw"] > 0 and b["u_tw"] > b["u_lw"] * 1.5]
    for b in growing[:2]:
        recs.append(f"<strong>{b['title']}</strong> demand is accelerating ({b['trend']}) — ensure stock keeps pace.")
    if dead:
        total_dead_value = sum(d["value"] for d in dead)
        recs.append(f"<strong>{fmt_money(total_dead_value)}</strong> in retail value is sitting idle (no sales in 2 weeks) — consider a flash sale or bundle offer.")

    html = '<h2>How to Improve</h2>'

    # --- Abandoned Checkout Recovery ---
    html += f"""
<div style="border:1px solid #e5e7eb;border-radius:8px;padding:18px 20px;margin-bottom:16px">
  <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:12px">
    <div style="font-size:22px;line-height:1">🛒</div>
    <div>
      <div style="font-weight:700;font-size:14px;color:#0f172a">Abandoned Checkout Recovery</div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px">
        {ab['count']} incomplete checkouts this week &nbsp;·&nbsp;
        <span style="font-weight:600;color:#b91c1c">{fmt_money(ab['value'])} revenue at risk</span>
      </div>
    </div>
  </div>"""

    if ab["top_products"]:
        html += """<table style="margin-top:0">
<tr>
  <th>#</th><th>Product</th><th>Brand</th><th style="text-align:right">Times Abandoned</th>
</tr>"""
        for i, p in enumerate(ab["top_products"], 1):
            html += f"""<tr>
  <td style="color:#9ca3af">{i}</td>
  <td style="font-size:13px">{p['title']}</td>
  <td style="font-size:12px;color:#6b7280">{p['vendor']}</td>
  <td style="text-align:right;font-weight:600">{p['count']}</td>
</tr>"""
        html += "</table>"

    html += f"""<p style="font-size:12px;color:#6b7280;margin:10px 0 0">
    → Enable abandoned checkout emails in
    <a href="https://business-mjgtrading.myshopify.com/admin/settings/notifications">Shopify Notifications</a>
    to recover this automatically.
  </p>
</div>"""

    # --- Customer Health ---
    new_bar = cust["new_pct"]
    ret_bar = cust["returning_pct"]
    new_color = "#b91c1c" if new_bar < 10 else "#166534"
    acq_note = (
        "Low new customer rate — consider acquisition campaigns (paid ads, referrals, influencers)."
        if new_bar < 10 else
        "Healthy mix of new and returning customers."
    )
    html += f"""
<div style="border:1px solid #e5e7eb;border-radius:8px;padding:18px 20px;margin-bottom:16px">
  <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:14px">
    <div style="font-size:22px;line-height:1">👥</div>
    <div>
      <div style="font-weight:700;font-size:14px;color:#0f172a">Customer Health</div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px">Based on {len(orders_tw)} orders this week</div>
    </div>
  </div>
  <div style="display:flex;gap:20px;margin-bottom:12px">
    <div style="flex:1;text-align:center;background:#f8fafc;border-radius:6px;padding:12px">
      <div style="font-size:22px;font-weight:700;color:{new_color}">{cust['new_pct']}%</div>
      <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em">New Customers</div>
      <div style="font-size:11px;color:#9ca3af">{cust['new']} orders</div>
    </div>
    <div style="flex:1;text-align:center;background:#f8fafc;border-radius:6px;padding:12px">
      <div style="font-size:22px;font-weight:700;color:#166534">{cust['returning_pct']}%</div>
      <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em">Returning</div>
      <div style="font-size:11px;color:#9ca3af">{cust['returning']} orders</div>
    </div>
    <div style="flex:1;text-align:center;background:#f8fafc;border-radius:6px;padding:12px">
      <div style="font-size:22px;font-weight:700;color:#374151">{cust['guest']}</div>
      <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em">Guest Orders</div>
      <div style="font-size:11px;color:#9ca3af">no account</div>
    </div>
  </div>
  <p style="font-size:12px;color:#6b7280;margin:0">→ {acq_note}</p>
</div>"""

    # --- Dead Stock ---
    if dead:
        total_dead_value = sum(d["value"] for d in dead)
        html += f"""
<div style="border:1px solid #e5e7eb;border-radius:8px;padding:18px 20px;margin-bottom:16px">
  <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:12px">
    <div style="font-size:22px;line-height:1">📦</div>
    <div>
      <div style="font-weight:700;font-size:14px;color:#0f172a">Idle Inventory — No Sales in 2 Weeks</div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px">
        <span style="font-weight:600;color:#92400e">{fmt_money(total_dead_value)}</span> in retail value sitting idle
      </div>
    </div>
  </div>
  <table style="margin-top:0">
  <tr><th>Brand</th><th style="text-align:right">Units</th><th style="text-align:right">Retail Value</th></tr>"""
        for d in dead:
            html += f"""<tr>
  <td style="font-weight:600">{d['brand']}</td>
  <td style="text-align:right">{d['stock']:,}</td>
  <td style="text-align:right">{fmt_money(d['value'])}</td>
</tr>"""
        html += """</table>
  <p style="font-size:12px;color:#6b7280;margin:10px 0 0">→ Consider a limited-time discount, bundle, or marketing push to move this inventory.</p>
</div>"""

    # --- Recommendations ---
    if recs:
        html += """
<div style="border:1px solid #e5e7eb;border-radius:8px;padding:18px 20px;margin-bottom:16px">
  <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:12px">
    <div style="font-size:22px;line-height:1">💡</div>
    <div style="font-weight:700;font-size:14px;color:#0f172a">Key Actions This Week</div>
  </div>
  <ul style="margin:0;padding-left:18px;font-size:13px;color:#374151;line-height:1.9">"""
        for r in recs:
            html += f"<li>{r}</li>"
        html += "</ul></div>"

    return html


def build_email(products, orders_tw, orders_lw, abandoned, now_est):
    logo_b64 = load_logo_b64()
    logo_tag = (
        f'<img src="data:image/jpeg;base64,{logo_b64}" alt="MJG Trading" '
        f'style="width:80px;height:80px;object-fit:contain;display:block">'
        if logo_b64 else '<span style="font-size:20px;font-weight:700;color:#0f172a">MJG Trading</span>'
    )

    week_end = now_est.strftime("%B %d, %Y")
    week_start_label = (now_est - timedelta(days=6)).strftime("%B %d")
    brands = build_brand_data(products, orders_tw, orders_lw)
    top_products = build_top_products(products, orders_tw)
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
  h2{{font-size:13px;font-weight:700;color:#0f172a;text-transform:uppercase;letter-spacing:.07em;margin:28px 0 10px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}}
  .kpi-row{{display:flex;gap:12px;margin-bottom:4px}}
  .kpi{{flex:1;background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:16px 18px}}
  .kpi-num{{font-size:24px;font-weight:700;color:#0f172a}}
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
  .alert-box{{background:#fef2f2;border-left:3px solid #dc2626;padding:12px 16px;margin:16px 0;border-radius:0 4px 4px 0;font-size:13px}}
  .alert-box strong{{color:#991b1b}}
  .prod-img{{width:120px;height:auto;border-radius:4px;border:1px solid #e5e7eb;display:block}}
  .no-img{{width:120px;height:70px;border-radius:4px;border:1px solid #e5e7eb;background:#f1f5f9;display:block}}
</style>
</head>
<body>
<div class="wrapper">

<div class="header">
  <div>{logo_tag}</div>
  <div class="header-right">
    <strong>Weekly Brand Intelligence Report</strong>
    {week_start_label} – {week_end}
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
        names = ", ".join(f'<strong>{b["title"]}</strong>' for b in urgent_brands)
        html += f"""
<div class="alert-box">
  ⚠ Reorder Alert ({len(urgent_brands)} brand{"s" if len(urgent_brands) > 1 else ""}): {names} — stock critically low or sold out.
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
  <td style="font-weight:600">{b['title']}
    <span style="color:#9ca3af;font-size:11px;font-weight:400">&nbsp;({b['product_count']} SKUs)</span></td>
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
  <th style="width:48px"></th>
  <th>#</th>
  <th>Product</th>
  <th>Brand</th>
  <th style="text-align:right">Units</th>
  <th style="text-align:right">Revenue</th>
</tr>
"""
    if top_products:
        for i, p in enumerate(top_products, 1):
            img_html = (
                f'<img src="{p["image"]}" class="prod-img" alt="">'
                if p.get("image") else '<span class="no-img"></span>'
            )
            html += f"""<tr>
  <td style="padding:6px 8px">{img_html}</td>
  <td style="color:#9ca3af;font-weight:600">{i}</td>
  <td style="font-weight:500">{p['title']}</td>
  <td style="color:#6b7280;font-size:12px">{p['vendor']}</td>
  <td style="text-align:right;font-weight:600">{p['units']}</td>
  <td style="text-align:right">{fmt_money(p['revenue'])}</td>
</tr>"""
    else:
        html += '<tr><td colspan="6" style="color:#9ca3af;font-style:italic">No sales recorded this week.</td></tr>'
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
            chg = (b["r_tw"] - b["r_lw"]) / b["r_lw"] * 100 if b["r_lw"] > 0 else None
            html += f"""<tr>
  <td style="font-weight:600">{b['title']}</td>
  <td style="text-align:right;font-weight:600">{fmt_money(b['r_tw'])}</td>
  <td style="text-align:right;color:#9ca3af">{fmt_money(b['r_lw'])}</td>
  <td style="text-align:center">{fmt_chg(chg)}</td>
</tr>"""
    else:
        html += '<tr><td colspan="4" style="color:#9ca3af;font-style:italic">No revenue data for this period.</td></tr>'
    html += "</table>"

    html += "<h2>Flags &amp; Recommendations</h2>"

    if dead_brands:
        html += '<p style="font-size:13px;color:#374151;margin:0 0 8px"><strong>No sales in 2+ weeks:</strong> '
        html += ", ".join(f'<strong>{b["title"]}</strong>' for b in dead_brands)
        html += " — consider a promotional push or inventory review.</p>"

    sold_out_with_demand = [b for b in brands if b["status"] == "SOLD OUT" and b["u_tw"] > 0]
    if sold_out_with_demand:
        html += '<p style="font-size:13px;color:#374151;margin:0 0 8px"><strong>Sold out with active demand:</strong> '
        html += ", ".join(f'<strong>{b["title"]}</strong> ({b["u_tw"]} units sold)' for b in sold_out_with_demand)
        html += " — high priority reorder.</p>"

    top_brand = max(brands, key=lambda b: b["r_tw"], default=None)
    if top_brand and top_brand["r_tw"] > 0:
        html += (
            f'<p style="font-size:13px;color:#374151;margin:0 0 8px">'
            f'<strong>Best performing brand:</strong> {top_brand["title"]} — '
            f'{fmt_money(top_brand["r_tw"])} revenue, {top_brand["u_tw"]} units sold.</p>'
        )

    growing = [b for b in brands if b["u_lw"] > 0 and b["u_tw"] > b["u_lw"] * 1.5]
    if growing:
        html += '<p style="font-size:13px;color:#374151;margin:0 0 8px"><strong>Accelerating demand (+50% vs last week):</strong> '
        html += ", ".join(f'<strong>{b["title"]}</strong> ({b["trend"]})' for b in growing)
        html += " — monitor stock levels closely.</p>"

    if not any([dead_brands, sold_out_with_demand, growing]):
        html += '<p style="color:#9ca3af;font-style:italic;font-size:13px">No significant flags this week.</p>'

    html += build_improvement_section(products, orders_tw, orders_lw, abandoned, brands)

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

    print("Fetching products...")
    products = client.get_all_products()
    print(f"  {len(products)} products")

    print("Fetching this week's orders...")
    orders_tw = client.get_orders_in_range(week_start, week_end)
    print(f"  {len(orders_tw)} orders this week")

    print("Fetching last week's orders...")
    orders_lw = client.get_orders_in_range(prev_week_start, week_start)
    print(f"  {len(orders_lw)} orders last week")

    print("Fetching abandoned checkouts...")
    abandoned = client.get_abandoned_checkouts_in_range(week_start, week_end)
    print(f"  {len(abandoned)} incomplete abandoned checkouts")

    html = build_email(products, orders_tw, orders_lw, abandoned, now_est)

    week_str = now_est.strftime("Week of %B %d, %Y")
    subject = f"MJG Trading — Weekly Brand Report · {week_str}"
    send_email(subject, html)
    print("Done.")


if __name__ == "__main__":
    main()
