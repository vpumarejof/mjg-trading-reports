#!/usr/bin/env python3
"""
MJG Trading — Weekly Empty Collections Report (for Adam)

Lists every collection that is still LIVE on the Online Store but has
0 units in stock, so it can be hidden or restocked.

Unlike collection_alert.py (which only fires at the moment a collection
goes from >0 to 0), this report lists ALL live empty collections every
week, including ones that were already empty.

Recipient: EMPTY_COLLECTIONS_EMAIL (defaults to adam@mjgtrading.com).
Usage:
  python reports/empty_collections_weekly.py            # send
  python reports/empty_collections_weekly.py --dry-run  # write preview HTML only
"""

import os
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from shopify_client import ShopifyClient
from email_utils import send_email

NY = ZoneInfo("America/New_York")
DEFAULT_RECIPIENT = "adam@mjgtrading.com"
STORE_URL = "https://business.mjgtrading.com"
ADMIN_URL = "https://business-mjgtrading.myshopify.com/admin"

PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 250, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      collections(first: 50) { nodes { id } }
      variants(first: 100) {
        nodes { inventoryQuantity inventoryItem { tracked } }
      }
    }
  }
}
"""

COLLECTIONS_QUERY = """
query Collections($cursor: String, $pub: ID!) {
  collections(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      legacyResourceId
      title
      handle
      updatedAt
      publishedOnPublication(publicationId: $pub)
    }
  }
}
"""


MENU_QUERY = """
{ menus(first: 50) { nodes { id handle items {
    title resourceId url
    items { title resourceId url items { title resourceId url } }
} } } }
"""


def main_menu_links(client):
    """Return (menu_legacy_id, {collection gid or handle: 'Parent > Child'}) for the main menu."""
    data = client.graphql(MENU_QUERY)
    menu = next((m for m in data["menus"]["nodes"] if m["handle"] == "main-menu"), None)
    if not menu:
        return None, {}
    links = {}

    def walk(items, path):
        for item in items:
            label = " > ".join(path + [item["title"]])
            if item.get("resourceId"):
                links.setdefault(item["resourceId"], label)
            url = item.get("url") or ""
            if "/collections/" in url:
                handle = url.split("/collections/")[1].split("?")[0].split("/")[0]
                links.setdefault(handle, label)
            walk(item.get("items") or [], path + [item["title"]])

    walk(menu["items"], [])
    return menu["id"].rsplit("/", 1)[-1], links


def online_store_publication_id(client):
    data = client.graphql("{ publications(first: 50) { nodes { id name } } }")
    for pub in data["publications"]["nodes"]:
        if pub["name"] == "Online Store":
            return pub["id"]
    raise RuntimeError("Online Store publication not found")


def paginate(client, query, key, variables=None):
    nodes, cursor = [], None
    while True:
        data = client.graphql(query, {**(variables or {}), "cursor": cursor})
        page = data[key]
        nodes.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"]["endCursor"]


def find_empty_live_collections(client):
    pub_id = online_store_publication_id(client)
    collections = paginate(client, COLLECTIONS_QUERY, "collections", {"pub": pub_id})
    live = {c["id"]: c for c in collections if c["publishedOnPublication"]}
    print(f"{len(collections)} collections, {len(live)} live on Online Store")

    products = paginate(client, PRODUCTS_QUERY, "products")
    print(f"{len(products)} active products")

    stats = {cid: {"products": 0, "units": 0, "untracked": 0} for cid in live}
    for p in products:
        units = untracked = 0
        for v in p["variants"]["nodes"]:
            if v["inventoryItem"] and v["inventoryItem"]["tracked"] is False:
                untracked += 1
            else:
                units += max(0, v["inventoryQuantity"] or 0)
        for col in p["collections"]["nodes"]:
            s = stats.get(col["id"])
            if s is not None:
                s["products"] += 1
                s["units"] += units
                s["untracked"] += untracked

    empty = []
    for cid, s in stats.items():
        # Untracked variants are always sellable, so the collection is not empty.
        if s["units"] == 0 and s["untracked"] == 0:
            col = live[cid]
            empty.append({
                **col,
                "active_products": s["products"],
                "reason": "No active products" if s["products"] == 0 else "0 units in stock",
            })
    menu_id, links = main_menu_links(client)
    for c in empty:
        c["menu_path"] = links.get(c["id"]) or links.get(c["handle"])
    empty.sort(key=lambda c: (c["menu_path"] is None, c["menu_path"] or "", c["title"].lower()))
    return empty, len(live), menu_id


def build_html(empty, live_count, now, menu_id):
    date_str = now.strftime("%A, %B %d, %Y")
    in_menu = [c for c in empty if c["menu_path"]]
    not_in_menu = [c for c in empty if not c["menu_path"]]
    menu_admin = f"{ADMIN_URL}/menus/{menu_id}" if menu_id else f"{ADMIN_URL}/menus"

    def status(c):
        if c["active_products"]:
            return f'{c["active_products"]} products, 0 units'
        return "No products"

    if not empty:
        body = '<p style="margin:0">All live collections have stock. Nothing to change this week.</p>'
    else:
        body = ""
        if in_menu:
            rows = "".join(f"""
      <tr>
        <td style="padding:9px 8px;border-bottom:1px solid #eee">
          <strong>{escape(c['title'])}</strong><br>
          <span style="color:#888;font-size:12px">{escape(c['menu_path'])}</span>
        </td>
        <td style="padding:9px 8px;border-bottom:1px solid #eee;font-size:13px">{status(c)}</td>
        <td style="padding:9px 8px;border-bottom:1px solid #eee;white-space:nowrap">
          <a href="{STORE_URL}/collections/{escape(c['handle'])}" style="color:#1a1a2e">View</a>
        </td>
      </tr>""" for c in in_menu)
            body += f"""
  <h2 style="font-size:16px;margin:0 0 6px">Still in the menu with no stock ({len(in_menu)})</h2>
  <p style="margin:0 0 12px;color:#555;font-size:13px">These menu links open an empty page.
  <a href="{menu_admin}" style="color:#dc3545;font-weight:bold">Open the main menu in Shopify</a> to remove them.</p>
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:28px">
    <thead><tr style="background:#f8f9fa;text-align:left">
      <th style="padding:8px">Collection and menu location</th><th style="padding:8px">Status</th><th style="padding:8px"></th>
    </tr></thead>
    <tbody>{rows}
    </tbody>
  </table>"""
        else:
            body += '<p style="margin:0 0 20px"><strong>No empty collections are linked in the menu.</strong></p>'
        if not_in_menu:
            names = ", ".join(escape(c["title"]) for c in not_in_menu)
            body += f"""
  <h2 style="font-size:15px;margin:0 0 6px;color:#555">Empty but not in the menu ({len(not_in_menu)})</h2>
  <p style="margin:0;color:#777;font-size:12px;line-height:1.6">No action needed. For reference: {names}</p>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;color:#333;max-width:700px;margin:0 auto;padding:20px">
  <div style="background:#1a1a2e;color:#fff;padding:20px 24px;border-radius:8px;margin-bottom:20px">
    <h1 style="margin:0;font-size:20px">Empty Collections: {len(in_menu)} still in the menu</h1>
    <p style="margin:6px 0 0;opacity:.8;font-size:13px">Weekly report · {date_str} · {live_count} live collections checked</p>
  </div>
  {body}
  <p style="color:#aaa;font-size:11px;margin-top:24px;text-align:center">
    Automatic weekly report · MJG Trading · <a href="{ADMIN_URL}/collections" style="color:#aaa">Shopify collections</a>
  </p>
</body></html>"""


def main():
    dry_run = "--dry-run" in sys.argv
    client = ShopifyClient()
    empty, live_count, menu_id = find_empty_live_collections(client)
    print(f"{len(empty)} live collections with no stock:")
    for c in empty:
        print(f"  - {c['title']} ({c['reason']}) menu: {c['menu_path'] or '-'}")

    now = datetime.now(NY)
    html = build_html(empty, live_count, now, menu_id)
    in_menu = sum(1 for c in empty if c["menu_path"])
    subject = f"Weekly Empty Collections: {in_menu} still in the menu · {now.strftime('%m/%d/%Y')}"

    if dry_run:
        out = Path(__file__).parent / "empty_collections_preview.html"
        out.write_text(html, encoding="utf-8")
        print(f"Dry run: preview written to {out}")
        return

    os.environ["REPORT_EMAIL"] = os.environ.get("EMPTY_COLLECTIONS_EMAIL") or DEFAULT_RECIPIENT
    send_email(subject, html)


if __name__ == "__main__":
    main()
