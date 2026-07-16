#!/usr/bin/env python3
"""
MJG Trading — Monthly Recap Report
Runs on the first business day of each month at 8 AM EDT via GitHub Actions.
"""

import base64
import os
import sys
import calendar
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from shopify_client import ShopifyClient
from email_utils import send_email

NY_TZ = ZoneInfo("America/New_York")
LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo.jpg"

VENDOR_EXCLUDE = {"OPEN BOX", "DROPSHIP", "SIGNATURE", "PRIMARY"}
VENDOR_ALIASES = {
    "RAG & BONE":         "RAG AND BONE",
    "YVES SAINT LAURENT": "SAINT LAURENT",
    "JUICY":              "JUICY COUTURE",
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_first_business_day():
    today = datetime.now(timezone.utc).astimezone(NY_TZ).date()
    if today.weekday() >= 5:
        return False
    d = date(today.year, today.month, 1)
    while d < today:
        if d.weekday() < 5:
            return False
        d += timedelta(days=1)
    return True


def month_range(year, month):
    # Calendar month boundaries in the shop's own timezone, not UTC — otherwise
    # the last few hours of a month (NY time) land in the wrong month's report.
    start = datetime(year, month, 1, tzinfo=NY_TZ)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=NY_TZ)
    else:
        end = datetime(year, month + 1, 1, tzinfo=NY_TZ)
    return start, end


def prev_month(year, month):
    if month == 1:
        return year - 1, 12
    return year, month - 1


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


def fmt_money(v):
    return f"${v:,.2f}"


def fmt_chg(val):
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    color = "#166534" if val >= 0 else "#b91c1c"
    return f'<span style="color:{color};font-size:12px">{sign}{val:.1f}%</span>'


# ── Data builders ─────────────────────────────────────────────────────────────

def tally_orders(orders):
    units, revenue = {}, {}
    for order in orders:
        for item in order["lineItems"]["nodes"]:
            if not item.get("product"):
                continue
            pid = item["product"]["legacyResourceId"]
            qty = item["currentQuantity"]
            price = float(item["discountedUnitPriceSet"]["shopMoney"]["amount"]) * qty
            units[pid] = units.get(pid, 0) + qty
            revenue[pid] = revenue.get(pid, 0.0) + price
    return units, revenue


def executive_summary(orders_lm, orders_pm):
    def agg(orders):
        rev = sum(float(o["currentTotalPriceSet"]["shopMoney"]["amount"]) for o in orders)
        cnt = len(orders)
        units = sum(
            item["currentQuantity"]
            for o in orders
            for item in o["lineItems"]["nodes"]
        )
        return rev, cnt, rev / cnt if cnt else 0, units

    r_lm, c_lm, aov_lm, u_lm = agg(orders_lm)
    r_pm, c_pm, aov_pm, u_pm = agg(orders_pm)

    def chg(a, b):
        return ((a - b) / b * 100) if b else None

    return {
        "r_lm": r_lm, "r_pm": r_pm, "r_chg": chg(r_lm, r_pm),
        "c_lm": c_lm, "c_pm": c_pm, "c_chg": chg(c_lm, c_pm),
        "aov_lm": aov_lm, "aov_pm": aov_pm, "aov_chg": chg(aov_lm, aov_pm),
        "u_lm": u_lm, "u_pm": u_pm, "u_chg": chg(u_lm, u_pm),
    }


def week_by_week(orders_lm, orders_pm, lm_year, lm_month, pm_year, pm_month):
    """Split each month into 5 week buckets (days 1-7, 8-14, 15-21, 22-28, 29+)."""
    def bucket(order, year, month):
        # Convert to shop-local time before reading the day-of-month, otherwise
        # orders near midnight get bucketed against the wrong day/week.
        dt = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00")).astimezone(NY_TZ)
        day = dt.day
        if day <= 7:   return 0
        if day <= 14:  return 1
        if day <= 21:  return 2
        if day <= 28:  return 3
        return 4

    def agg_buckets(orders, year, month):
        buckets = [{"rev": 0.0, "cnt": 0} for _ in range(5)]
        for o in orders:
            b = bucket(o, year, month)
            buckets[b]["rev"] += float(o["currentTotalPriceSet"]["shopMoney"]["amount"])
            buckets[b]["cnt"] += 1
        return buckets

    lm_days = calendar.monthrange(lm_year, lm_month)[1]
    pm_days = calendar.monthrange(pm_year, pm_month)[1]
    lm_b = agg_buckets(orders_lm, lm_year, lm_month)
    pm_b = agg_buckets(orders_pm, pm_year, pm_month)

    labels = ["Days 1–7", "Days 8–14", "Days 15–21", "Days 22–28", f"Days 29–{lm_days}"]
    weeks = []
    for i, label in enumerate(labels):
        if i == 4 and lm_days < 29 and pm_days < 29:
            continue
        chg = None
        if pm_b[i]["rev"] > 0:
            chg = (lm_b[i]["rev"] - pm_b[i]["rev"]) / pm_b[i]["rev"] * 100
        weeks.append({
            "label": label,
            "lm_rev": lm_b[i]["rev"], "lm_cnt": lm_b[i]["cnt"],
            "pm_rev": pm_b[i]["rev"], "pm_cnt": pm_b[i]["cnt"],
            "chg": chg,
        })
    return weeks


def build_brand_data(products, orders_lm, orders_pm, days_lm):
    units_lm, rev_lm = tally_orders(orders_lm)
    units_pm, rev_pm = tally_orders(orders_pm)

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
        u_lm = sum(units_lm.get(p["legacyResourceId"], 0) for p in prods)
        u_pm = sum(units_pm.get(p["legacyResourceId"], 0) for p in prods)
        r_lm = sum(rev_lm.get(p["legacyResourceId"], 0.0) for p in prods)
        r_pm = sum(rev_pm.get(p["legacyResourceId"], 0.0) for p in prods)

        avg_daily = u_lm / days_lm
        if stock == 0:
            status = "SOLD OUT"
        elif avg_daily == 0:
            status = "MONITOR"
        else:
            days = stock / avg_daily
            if days < 7:    status = "REORDER NOW"
            elif days < 14: status = "REORDER SOON"
            elif days < 45: status = "ADEQUATE"
            else:           status = "OK"

        if u_pm == 0:
            trend = f"+{u_lm}" if u_lm > 0 else "—"
        elif u_lm > u_pm:
            trend = f"+{round((u_lm - u_pm) / u_pm * 100)}%"
        elif u_lm < u_pm:
            trend = f"−{round((u_pm - u_lm) / u_pm * 100)}%"
        else:
            trend = "="

        days_of_stock = None if avg_daily == 0 else round(stock / avg_daily)

        brands.append({
            "title": vendor, "product_count": len(prods),
            "stock": stock, "u_lm": u_lm, "u_pm": u_pm,
            "r_lm": r_lm, "r_pm": r_pm,
            "days_of_stock": days_of_stock, "trend": trend, "status": status,
        })

    return sorted(brands, key=lambda b: -b["r_lm"])


def build_top_products(products, orders_lm):
    product_meta = {
        p["legacyResourceId"]: {
            "title": p["title"],
            "vendor": normalize_vendor(p["vendor"]),
            "image": thumb(p["featuredImage"]["url"]) if p.get("featuredImage") else None,
        }
        for p in products
        if p["status"] == "ACTIVE" and normalize_vendor(p["vendor"]) not in VENDOR_EXCLUDE
    }

    sold = {}
    for order in orders_lm:
        for item in order["lineItems"]["nodes"]:
            if not item.get("product"):
                continue
            pid = item["product"]["legacyResourceId"]
            if pid not in product_meta:
                continue
            if pid not in sold:
                sold[pid] = {**product_meta[pid], "legacy_id": pid, "units": 0, "revenue": 0.0}
            sold[pid]["units"] += item["currentQuantity"]
            sold[pid]["revenue"] += float(item["discountedUnitPriceSet"]["shopMoney"]["amount"]) * item["currentQuantity"]

    return sorted(sold.values(), key=lambda x: -x["units"])[:20]


def customer_breakdown(orders):
    new_c     = sum(1 for o in orders if o.get("customer") and int(o["customer"]["numberOfOrders"]) == 1)
    returning = sum(1 for o in orders if o.get("customer") and int(o["customer"]["numberOfOrders"]) > 1)
    guest     = sum(1 for o in orders if not o.get("customer"))
    total = len(orders) or 1
    return {
        "new": new_c, "returning": returning, "guest": guest,
        "new_pct": round(new_c / total * 100),
        "returning_pct": round(returning / total * 100),
    }


def abandoned_summary(abandoned):
    total_value = sum(float(c["totalLineItemsPriceSet"]["shopMoney"]["amount"]) for c in abandoned)
    prods = {}
    for c in abandoned:
        for item in c["lineItems"]["nodes"]:
            if not item.get("variant") or not item["variant"].get("product"):
                continue
            pid = item["variant"]["product"]["legacyResourceId"]
            title = item["variant"]["product"]["title"]
            vendor = normalize_vendor(item["variant"]["product"].get("vendor", ""))
            prods.setdefault(pid, {"title": title, "vendor": vendor, "count": 0})
            prods[pid]["count"] += 1
    top = sorted(prods.values(), key=lambda x: -x["count"])[:5]
    return {"count": len(abandoned), "value": total_value, "top_products": top}


def dead_stock_brands(products, orders_lm, orders_pm):
    sold_pids = set()
    for o in orders_lm + orders_pm:
        for item in o["lineItems"]["nodes"]:
            if item.get("product"):
                sold_pids.add(item["product"]["legacyResourceId"])

    brand_dead = {}
    for p in products:
        if p["status"] != "ACTIVE" or p["legacyResourceId"] in sold_pids:
            continue
        vendor = normalize_vendor(p["vendor"])
        if vendor in VENDOR_EXCLUDE or not vendor:
            continue
        stock = sum(max(0, v["inventoryQuantity"] or 0) for v in p["variants"]["nodes"])
        if stock == 0:
            continue
        price = float(p["priceRangeV2"]["minVariantPrice"]["amount"]) if p.get("priceRangeV2") else 0
        brand_dead.setdefault(vendor, {"stock": 0, "value": 0.0})
        brand_dead[vendor]["stock"] += stock
        brand_dead[vendor]["value"] += stock * price

    result = [{"brand": k, **v} for k, v in brand_dead.items()]
    return sorted(result, key=lambda x: -x["value"])[:10]


def admin_prod_url(lid):
    return f"https://business-mjgtrading.myshopify.com/admin/products/{lid}"


# ── Email builder ─────────────────────────────────────────────────────────────

def build_email(products, orders_lm, orders_pm, abandoned, lm_year, lm_month, days_lm):
    logo_b64 = load_logo_b64()
    logo_tag = (
        f'<img src="data:image/jpeg;base64,{logo_b64}" alt="MJG Trading" '
        f'style="width:80px;height:80px;object-fit:contain;display:block">'
        if logo_b64 else '<span style="font-size:20px;font-weight:700">MJG Trading</span>'
    )

    lm_name = datetime(lm_year, lm_month, 1).strftime("%B %Y")
    pm_year, pm_month = prev_month(lm_year, lm_month)
    pm_name = datetime(pm_year, pm_month, 1).strftime("%B %Y")

    summary  = executive_summary(orders_lm, orders_pm)
    weeks    = week_by_week(orders_lm, orders_pm, lm_year, lm_month, pm_year, pm_month)
    brands   = build_brand_data(products, orders_lm, orders_pm, days_lm)
    top_prod = build_top_products(products, orders_lm)
    cust     = customer_breakdown(orders_lm)
    ab       = abandoned_summary(abandoned)
    dead     = dead_stock_brands(products, orders_lm, orders_pm)

    urgent = [b for b in brands if b["status"] in ("REORDER NOW", "SOLD OUT")]

    CSS = """
  body{font-family:'Segoe UI',Arial,sans-serif;color:#1f2937;max-width:860px;margin:0 auto;padding:0;background:#f3f4f6}
  .wrapper{background:#fff;max-width:860px;margin:0 auto}
  .header{background:#ffffff;border-bottom:1px solid #e5e7eb;padding:24px 32px;display:flex;align-items:center;justify-content:space-between}
  .header-right{text-align:right;color:#6b7280;font-size:13px;line-height:1.6}
  .header-right strong{color:#0f172a;font-size:15px;display:block}
  .body{padding:28px 32px}
  h2{font-size:13px;font-weight:700;color:#0f172a;text-transform:uppercase;letter-spacing:.07em;margin:28px 0 10px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}
  .kpi-row{display:flex;gap:12px;margin-bottom:4px}
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
  .footer{background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 32px;font-size:11px;color:#9ca3af;text-align:center}
  .alert-box{background:#fef2f2;border-left:3px solid #dc2626;padding:12px 16px;margin:16px 0;border-radius:0 4px 4px 0;font-size:13px}
  .card{border:1px solid #e5e7eb;border-radius:8px;padding:18px 20px;margin-bottom:16px}
  .card-header{display:flex;align-items:flex-start;gap:14px;margin-bottom:12px}
  .card-icon{font-size:22px;line-height:1}
  .card-title{font-weight:700;font-size:14px;color:#0f172a}
  .card-sub{font-size:13px;color:#6b7280;margin-top:2px}
  .card-note{font-size:12px;color:#6b7280;margin:10px 0 0}
  .stat-box{flex:1;text-align:center;background:#f8fafc;border-radius:6px;padding:12px}
  .stat-num{font-size:22px;font-weight:700}
  .stat-label{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em}
  .stat-sub{font-size:11px;color:#9ca3af}
  .prod-img{width:120px;height:auto;border-radius:4px;border:1px solid #e5e7eb;display:block}
  .no-img{width:120px;height:70px;border-radius:4px;border:1px solid #e5e7eb;background:#f1f5f9;display:block}
"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{CSS}</style></head>
<body>
<div class="wrapper">

<div class="header">
  <div>{logo_tag}</div>
  <div class="header-right">
    <strong>MONTHLY SALES REPORT - MJG TRADING</strong>
    {lm_name}
  </div>
</div>

<div class="body">
"""

    # ── 1. Executive Summary ──────────────────────────────────────────────────
    html += f"""
<h2>Executive Summary — {lm_name}</h2>
<div class="kpi-row">
  <div class="kpi">
    <div class="kpi-num">{fmt_money(summary['r_lm'])}</div>
    <div class="kpi-label">Total Revenue</div>
    <div class="kpi-sub">vs {fmt_money(summary['r_pm'])} in {pm_name} &nbsp;{fmt_chg(summary['r_chg'])}</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{summary['c_lm']:,}</div>
    <div class="kpi-label">Orders</div>
    <div class="kpi-sub">vs {summary['c_pm']:,} in {pm_name} &nbsp;{fmt_chg(summary['c_chg'])}</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{fmt_money(summary['aov_lm'])}</div>
    <div class="kpi-label">Avg Order Value</div>
    <div class="kpi-sub">vs {fmt_money(summary['aov_pm'])} &nbsp;{fmt_chg(summary['aov_chg'])}</div>
  </div>
  <div class="kpi">
    <div class="kpi-num">{summary['u_lm']:,}</div>
    <div class="kpi-label">Units Sold</div>
    <div class="kpi-sub">vs {summary['u_pm']:,} &nbsp;{fmt_chg(summary['u_chg'])}</div>
  </div>
</div>
"""

    # ── 2. Last Month vs Prior Month week-by-week ─────────────────────────────
    html += f"""
<h2>{lm_name} vs {pm_name} — Week by Week</h2>
<table>
<tr>
  <th>Period</th>
  <th style="text-align:right">{lm_name} Revenue</th>
  <th style="text-align:right">{lm_name} Orders</th>
  <th style="text-align:right">{pm_name} Revenue</th>
  <th style="text-align:right">{pm_name} Orders</th>
  <th style="text-align:center">Change</th>
</tr>
"""
    total_lm_rev = sum(w["lm_rev"] for w in weeks)
    total_pm_rev = sum(w["pm_rev"] for w in weeks)
    for w in weeks:
        html += f"""<tr>
  <td style="font-weight:600">{w['label']}</td>
  <td style="text-align:right">{fmt_money(w['lm_rev'])}</td>
  <td style="text-align:right">{w['lm_cnt']}</td>
  <td style="text-align:right;color:#9ca3af">{fmt_money(w['pm_rev'])}</td>
  <td style="text-align:right;color:#9ca3af">{w['pm_cnt']}</td>
  <td style="text-align:center">{fmt_chg(w['chg'])}</td>
</tr>"""
    total_chg = (total_lm_rev - total_pm_rev) / total_pm_rev * 100 if total_pm_rev else None
    html += f"""<tr style="background:#f8fafc;font-weight:700">
  <td>Total</td>
  <td style="text-align:right">{fmt_money(total_lm_rev)}</td>
  <td style="text-align:right">{summary['c_lm']:,}</td>
  <td style="text-align:right;color:#9ca3af">{fmt_money(total_pm_rev)}</td>
  <td style="text-align:right;color:#9ca3af">{summary['c_pm']:,}</td>
  <td style="text-align:center">{fmt_chg(total_chg)}</td>
</tr>"""
    html += "</table>"

    # ── 3. Brand Performance ──────────────────────────────────────────────────
    if urgent:
        names = ", ".join(f'<strong>{b["title"]}</strong>' for b in urgent)
        html += f'<div class="alert-box">⚠ Reorder Alert: {names} — stock critically low or sold out.</div>'

    html += f"""
<h2>Brand Performance — {lm_name}</h2>
<table>
<tr>
  <th>Brand</th>
  <th style="text-align:right">Revenue</th>
  <th style="text-align:right">vs {pm_name}</th>
  <th style="text-align:right">Units Sold</th>
  <th style="text-align:center">Trend</th>
  <th style="text-align:right">Stock</th>
  <th style="text-align:right">Days Left</th>
  <th>Status</th>
</tr>
"""
    for b in brands:
        rev_chg = (b["r_lm"] - b["r_pm"]) / b["r_pm"] * 100 if b["r_pm"] > 0 else None
        days_disp = str(b["days_of_stock"]) if b["days_of_stock"] is not None else "—"
        style = STATUS_STYLE.get(b["status"], "")
        html += f"""<tr>
  <td style="font-weight:600">{b['title']}
    <span style="color:#9ca3af;font-size:11px;font-weight:400">&nbsp;({b['product_count']} SKUs)</span></td>
  <td style="text-align:right;font-weight:600">{fmt_money(b['r_lm'])}</td>
  <td style="text-align:right">{fmt_chg(rev_chg)}</td>
  <td style="text-align:right">{b['u_lm']}</td>
  <td style="text-align:center">{b['trend']}</td>
  <td style="text-align:right">{b['stock']:,}</td>
  <td style="text-align:right">{days_disp}</td>
  <td style="{style}">{b['status']}</td>
</tr>"""
    html += "</table>"

    # ── 4. Top 20 Products ────────────────────────────────────────────────────
    html += f"<h2>Top 20 Products — {lm_name}</h2>"
    html += """<table>
<tr>
  <th style="width:130px"></th><th>#</th><th>Product</th>
  <th>Brand</th>
  <th style="text-align:right">Units</th>
  <th style="text-align:right">Revenue</th>
</tr>"""
    for i, p in enumerate(top_prod, 1):
        img_html = (
            f'<img src="{p["image"]}" class="prod-img" alt="">'
            if p.get("image") else '<span class="no-img"></span>'
        )
        html += f"""<tr>
  <td style="padding:6px 8px">{img_html}</td>
  <td style="color:#9ca3af;font-weight:600">{i}</td>
  <td><a href="{admin_prod_url(p['legacy_id'])}" style="font-weight:500">{p['title']}</a></td>
  <td style="color:#6b7280;font-size:12px">{p['vendor']}</td>
  <td style="text-align:right;font-weight:600">{p['units']}</td>
  <td style="text-align:right">{fmt_money(p['revenue'])}</td>
</tr>"""
    html += "</table>"

    # ── 5. How to Improve ─────────────────────────────────────────────────────
    html += "<h2>How to Improve</h2>"

    # Abandoned checkouts
    html += f"""
<div class="card">
  <div class="card-header">
    <div class="card-icon">🛒</div>
    <div>
      <div class="card-title">Abandoned Checkout Recovery — {lm_name}</div>
      <div class="card-sub">{ab['count']} incomplete checkouts &nbsp;·&nbsp;
        <span style="font-weight:600;color:#b91c1c">{fmt_money(ab['value'])} revenue at risk</span>
      </div>
    </div>
  </div>"""
    if ab["top_products"]:
        html += """<table style="margin-top:0">
<tr><th>#</th><th>Product</th><th>Brand</th><th style="text-align:right">Times Abandoned</th></tr>"""
        for i, p in enumerate(ab["top_products"], 1):
            html += f"""<tr>
  <td style="color:#9ca3af">{i}</td>
  <td style="font-size:13px">{p['title']}</td>
  <td style="font-size:12px;color:#6b7280">{p['vendor']}</td>
  <td style="text-align:right;font-weight:600">{p['count']}</td>
</tr>"""
        html += "</table>"
    html += f"""<p class="card-note">→ Enable abandoned checkout emails in
    <a href="https://business-mjgtrading.myshopify.com/admin/settings/notifications">Shopify Notifications</a>
    to recover this automatically.</p>
</div>"""

    # Customer health
    new_color = "#b91c1c" if cust["new_pct"] < 10 else "#166534"
    acq_note = (
        "Low new customer rate — consider acquisition campaigns for next month (paid ads, referrals, influencers)."
        if cust["new_pct"] < 10 else
        "Healthy new customer rate. Keep up acquisition efforts."
    )
    html += f"""
<div class="card">
  <div class="card-header">
    <div class="card-icon">👥</div>
    <div>
      <div class="card-title">Customer Health — {lm_name}</div>
      <div class="card-sub">Based on {len(orders_lm):,} orders</div>
    </div>
  </div>
  <div style="display:flex;gap:16px;margin-bottom:12px">
    <div class="stat-box">
      <div class="stat-num" style="color:{new_color}">{cust['new_pct']}%</div>
      <div class="stat-label">New Customers</div>
      <div class="stat-sub">{cust['new']:,} orders</div>
    </div>
    <div class="stat-box">
      <div class="stat-num" style="color:#166534">{cust['returning_pct']}%</div>
      <div class="stat-label">Returning</div>
      <div class="stat-sub">{cust['returning']:,} orders</div>
    </div>
    <div class="stat-box">
      <div class="stat-num" style="color:#374151">{cust['guest']:,}</div>
      <div class="stat-label">Guest Orders</div>
      <div class="stat-sub">no account</div>
    </div>
  </div>
  <p class="card-note">→ {acq_note}</p>
</div>"""

    # Dead stock
    if dead:
        total_dead = sum(d["value"] for d in dead)
        html += f"""
<div class="card">
  <div class="card-header">
    <div class="card-icon">📦</div>
    <div>
      <div class="card-title">Idle Inventory — No Sales in 2 Months</div>
      <div class="card-sub"><span style="font-weight:600;color:#92400e">{fmt_money(total_dead)}</span> in retail value sitting idle</div>
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
  <p class="card-note">→ Consider a month-end promotion, bundle, or price adjustment to move this inventory.</p>
</div>"""

    # Auto-generated key actions
    recs = []
    if ab["count"] > 0:
        recs.append(f"<strong>{ab['count']} abandoned checkouts</strong> cost you {fmt_money(ab['value'])} last month — activate Shopify's abandoned checkout email to recover this automatically.")
    if cust["new_pct"] < 10:
        recs.append(f"Only <strong>{cust['new_pct']}% new customers</strong> in {lm_name} — set a goal to reach at least 15% next month through paid acquisition or referral programs.")
    elif cust["returning_pct"] >= 80:
        recs.append(f"<strong>{cust['returning_pct']}% returning customers</strong> — excellent loyalty. Consider a VIP tier or early access program to reward your best buyers.")
    top_brand = max(brands, key=lambda b: b["r_lm"], default=None)
    if top_brand and top_brand["r_lm"] > 0:
        recs.append(f"<strong>{top_brand['title']}</strong> was your top brand with {fmt_money(top_brand['r_lm'])} revenue — prioritize stock and visibility for this brand next month.")
    for b in urgent[:2]:
        recs.append(f"<strong>{b['title']}</strong> is {b['status'].lower()} — place a reorder immediately to avoid lost sales.")
    declining = [b for b in brands if b["u_pm"] > 0 and b["u_lm"] < b["u_pm"] * 0.7 and b["u_lm"] > 0]
    for b in declining[:2]:
        recs.append(f"<strong>{b['title']}</strong> revenue dropped {b['trend']} month-over-month — investigate pricing, stock availability, or marketing exposure.")
    if dead:
        total_dead = sum(d["value"] for d in dead)
        recs.append(f"<strong>{fmt_money(total_dead)}</strong> in idle inventory across {len(dead)} brands — a targeted end-of-month promotion could unlock significant cash flow.")

    if recs:
        html += """
<div class="card">
  <div class="card-header">
    <div class="card-icon">💡</div>
    <div class="card-title">Key Actions for Next Month</div>
  </div>
  <ul style="margin:0;padding-left:18px;font-size:13px;color:#374151;line-height:1.9">"""
        for r in recs:
            html += f"<li>{r}</li>"
        html += "</ul></div>"

    html += f"""
</div>
<div class="footer">
  MJG Trading &nbsp;·&nbsp; Monthly Recap Report &nbsp;·&nbsp;
  <a href="https://business-mjgtrading.myshopify.com/admin">Shopify Admin</a>
  &nbsp;·&nbsp; Sent automatically on the first business day of each month
</div>
</div>
</body></html>"""

    return html


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    force_run = os.environ.get("FORCE_RUN", "").lower() == "true"
    if not force_run and not is_first_business_day():
        print("Not the first business day of the month — skipping.")
        return

    now_utc = datetime.now(timezone.utc)
    today   = now_utc.astimezone(NY_TZ).date()

    lm_year  = today.year if today.month > 1 else today.year - 1
    lm_month = today.month - 1 if today.month > 1 else 12
    days_lm  = calendar.monthrange(lm_year, lm_month)[1]

    pm_year, pm_month = prev_month(lm_year, lm_month)

    lm_start, lm_end = month_range(lm_year, lm_month)
    pm_start, pm_end = month_range(pm_year, pm_month)

    lm_name = datetime(lm_year, lm_month, 1).strftime("%B %Y")
    print(f"Starting MJG Trading monthly recap for {lm_name}...")

    client = ShopifyClient()

    print("Fetching products...")
    products = client.get_all_products()
    print(f"  {len(products)} products")

    print(f"Fetching orders for {lm_name}...")
    orders_lm = client.get_orders_in_range(lm_start, lm_end)
    print(f"  {len(orders_lm)} orders")

    print(f"Fetching orders for prior month...")
    orders_pm = client.get_orders_in_range(pm_start, pm_end)
    print(f"  {len(orders_pm)} orders")

    print("Fetching abandoned checkouts...")
    abandoned = client.get_abandoned_checkouts_in_range(lm_start, lm_end)
    print(f"  {len(abandoned)} incomplete abandoned checkouts")

    html = build_email(products, orders_lm, orders_pm, abandoned, lm_year, lm_month, days_lm)

    subject = f"MONTHLY SALES REPORT - MJG TRADING · {lm_name}"
    send_email(subject, html)
    print("Done.")


if __name__ == "__main__":
    main()
