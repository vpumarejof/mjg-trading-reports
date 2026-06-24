import os
import time
import requests


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
              totalPriceSet { shopMoney { amount currencyCode } }
              lineItems(first: 100) {
                nodes {
                  quantity
                  originalUnitPriceSet { shopMoney { amount } }
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
        filter_q = f"created_at:>={start_date.strftime('%Y-%m-%dT%H:%M:%SZ')} created_at:<{end_date.strftime('%Y-%m-%dT%H:%M:%SZ')} status:any financial_status:paid"
        orders = []
        cursor = None
        while True:
            data = self.graphql(query, {"cursor": cursor, "query": filter_q})
            page = data["orders"]
            orders.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return orders

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
