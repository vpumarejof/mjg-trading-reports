import os
import time
import requests
from datetime import timezone


class ShopifyClient:
    def __init__(self):
        self.store = os.environ.get("SHOPIFY_STORE", "business-mjgtrading.myshopify.com")
        self.token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
        if not self.token:
            raise ValueError("SHOPIFY_ACCESS_TOKEN not set")
        self.graphql_url = f"https://{self.store}/admin/api/2025-01/graphql.json"
        self.headers = {
            "X-Shopify-Access-Token": self.token,
            "Content-Type": "application/json",
        }

    def graphql(self, query, variables=None):
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        while True:
            resp = requests.post(self.graphql_url, json=payload, headers=self.headers, timeout=30)
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", 2)))
                continue
            resp.raise_for_status()
            result = resp.json()
            if "errors" in result:
                raise RuntimeError(f"GraphQL errors: {result['errors']}")
            return result["data"]

    def get_all_collections_with_products(self):
        query = """
        query GetCollections($cursor: String) {
          collections(first: 50, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              legacyResourceId
              title
              handle
              products(first: 250) {
                nodes {
                  id
                  legacyResourceId
                  title
                  status
                  createdAt
                  variants(first: 100) {
                    nodes {
                      id
                      sku
                      inventoryQuantity
                    }
                  }
                }
              }
            }
          }
        }
        """
        collections = []
        cursor = None
        while True:
            data = self.graphql(query, {"cursor": cursor})
            page = data["collections"]
            collections.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return collections

    def get_orders_in_range(self, start_date, end_date):
        query = """
        query GetOrders($cursor: String, $query: String) {
          orders(first: 250, after: $cursor, query: $query, sortKey: CREATED_AT) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              name
              createdAt
              cancelledAt
              displayFinancialStatus
              totalPriceSet { shopMoney { amount currencyCode } }
              currentTotalPriceSet { shopMoney { amount currencyCode } }
              customer { numberOfOrders }
              lineItems(first: 100) {
                nodes {
                  quantity
                  currentQuantity
                  originalUnitPriceSet { shopMoney { amount } }
                  discountedUnitPriceSet { shopMoney { amount } }
                  product {
                    id
                    legacyResourceId
                    title
                  }
                  variant { id sku }
                }
              }
            }
          }
        }
        """
        # No financial_status filter: Shopify's own sales reports count orders
        # regardless of payment status (pending/authorized net-terms orders included),
        # they just exclude cancelled orders — which we filter client-side below,
        # since cancelling doesn't guarantee a financial_status change.
        #
        # Two things matter here or the date boundary silently breaks:
        # 1. Values must be quoted — the colons in "HH:MM:SS" collide with
        #    Shopify's own field:value search syntax and corrupt unquoted filters
        #    (the upper bound gets ignored entirely, matching orders far outside the range).
        # 2. Values must be converted to true UTC before formatting — strftime
        #    prints an aware datetime's own local wall-clock time regardless of
        #    tzinfo, so a naive "+Z" suffix on a non-UTC datetime mislabels it.
        start_utc = start_date.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_utc = end_date.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        filter_q = (
            f'created_at:>="{start_utc}" '
            f'created_at:<"{end_utc}" '
            f"status:any"
        )
        orders = []
        cursor = None
        while True:
            data = self.graphql(query, {"cursor": cursor, "query": filter_q})
            page = data["orders"]
            orders.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return [o for o in orders if not o["cancelledAt"]]

    def get_abandoned_checkouts_in_range(self, start_date, end_date):
        query = """
        query GetAbandoned($cursor: String, $query: String) {
          abandonedCheckouts(first: 250, after: $cursor, query: $query) {
            pageInfo { hasNextPage endCursor }
            nodes {
              createdAt
              completedAt
              totalLineItemsPriceSet { shopMoney { amount } }
              lineItems(first: 50) {
                nodes {
                  title
                  quantity
                  variant {
                    product { legacyResourceId title vendor }
                  }
                }
              }
            }
          }
        }
        """
        start_utc = start_date.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        end_utc = end_date.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        filter_q = (
            f'created_at:>="{start_utc}" '
            f'created_at:<"{end_utc}"'
        )
        checkouts = []
        cursor = None
        while True:
            data = self.graphql(query, {"cursor": cursor, "query": filter_q})
            page = data["abandonedCheckouts"]
            checkouts.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return [c for c in checkouts if not c["completedAt"]]

    def get_all_products(self):
        query = """
        query GetProducts($cursor: String) {
          products(first: 250, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              legacyResourceId
              title
              status
              vendor
              createdAt
              featuredImage { url }
              priceRangeV2 { minVariantPrice { amount } }
              variants(first: 100) {
                nodes {
                  id
                  sku
                  inventoryQuantity
                }
              }
            }
          }
        }
        """
        products = []
        cursor = None
        while True:
            data = self.graphql(query, {"cursor": cursor})
            page = data["products"]
            products.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return products
