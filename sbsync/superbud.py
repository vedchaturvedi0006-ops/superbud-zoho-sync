"""Superbud GraphQL client. All queries live here so they are easy to adjust if
Superbud changes its beta API.

Every Superbud query needs intentCategory + queryIntent (a plain description,
no customer names or license numbers in it)."""
from . import config
from .http import ApiError, request

# Superbud allows 50 requests / 10 s and 250 / 60 s per user. 0.3 s between
# calls keeps us at ~200 per minute at most.
MIN_INTERVAL = 0.3
PAGE_SIZE = 25  # small pages keep each query under Superbud's query-size limit

ADDRESS = "line1 line2 city state zip country"

def location_fields(distributor_id):
    # assignedSalesRep needs the distributor id as an argument
    return f"""
  id name
  customer {{ id name website deletedAt }}
  address {{ {ADDRESS} }}
  businessAddress {{ {ADDRESS} }}
  assignedSalesRep(distributorId: "{distributor_id}") {{ id email }}
"""

PRODUCT_FIELDS = """
  id name description category subCategory
  brand { name }
  collections(pagination: {first: 5}) { edges { node { id name } } }
  options {
    id name sku basePriceDollars msrpDollars displayQuantityAvailable displayQuantityReserved packSize packType
  }
"""

PERSON = "id firstName lastName email phoneNumber"

ORDER_FIELDS = f"""
  id orderNumber status approvalState version createdAt approvedAt dueDate source
  paymentTerms {{ billOn delinquentAfterDays paymentTerm preferredPaymentMethod }}
  submittedAt shippedAt deliveredAt cancelledAt fullyPaidAt
  balanceDueDollars isUnpaid isPastDue daysPastDue
  subtotal total shippingFee localTax stateTax credits manualAdjustments
  customer {{ id name }}
  customerLocation {{ id name }}
  buyerRep {{ {PERSON} }}
  placedByUser {{ {PERSON} }}
  salesRep {{ id email }}
  history {{ id occurredAt eventJson }}
  payments {{ id amountDollars method paymentDate referenceNumber notes }}
  shippingAddress {{ {ADDRESS} }}
  lineItems {{
    id quantity basePriceDollars priceDollars discountDollars totalDollars
    option {{ name currentModel {{ id }} }}
  }}
"""


class Superbud:
    def __init__(self):
        if not config.SUPERBUD_API_KEY():
            raise ApiError("SUPERBUD_API_KEY is missing from .env")
        self._distributor_id = config.SUPERBUD_DISTRIBUTOR_ID()

    def query(self, text, variables=None):
        resp = request(
            "POST", config.SUPERBUD_API_URL(),
            headers={"Authorization": f"Bearer {config.SUPERBUD_API_KEY()}", "Content-Type": "application/json"},
            json={"query": text, "variables": variables or {}},
            min_interval=MIN_INTERVAL,
        )
        try:
            body = resp.json()
        except ValueError:
            raise ApiError(f"Superbud returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}", resp.status_code)
        if resp.status_code >= 400 or body.get("errors"):
            raise ApiError(f"Superbud error (HTTP {resp.status_code}): {body.get('errors') or body}", resp.status_code, body)
        return body["data"]

    def _paginate(self, root, fields, intent, where=None, category="LIST"):
        """Read every page of a connection query such as orders or customerLocations."""
        items, after, size = [], None, PAGE_SIZE
        while True:
            text = f"""
            query Page($after: String, $where: {self._where_type(root)}) {{
              {root}(intentCategory: {category}, queryIntent: "{intent}",
                     pagination: {{first: {size}, after: $after}}, where: $where) {{
                edges {{ node {{ {fields} }} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}"""
            try:
                data = self.query(text, {"after": after, "where": where})[root]
            except ApiError as exc:
                # Superbud rejects queries that are too "wide"; retry with smaller pages
                if size > 1 and any(w in str(exc).lower() for w in ("breadth", "complex", "too large", "cost")):
                    size = max(1, size // 2)
                    continue
                raise
            items.extend(edge["node"] for edge in data.get("edges") or [])
            page = data.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return items
            after = page.get("endCursor")

    @staticmethod
    def _where_type(root):
        return {
            "orders": "OrdersWhereFilter",
            "customerLocations": "CustomerLocationsWhereFilter",
            "products": "ProductsWhereFilter",
        }[root]

    # -- objects ------------------------------------------------------------
    def distributor_id(self):
        if self._distributor_id:
            return self._distributor_id
        data = self.query("""
        { sessionScope(intentCategory: LOOKUP,
                       queryIntent: "Find the distributor this API key belongs to for CRM sync") {
            distributors(pagination: {first: 10}) { edges { node { id name } } }
        } }""")
        nodes = [e["node"] for e in data["sessionScope"]["distributors"]["edges"]]
        if len(nodes) != 1:
            raise ApiError(f"API key sees {len(nodes)} distributors; set SUPERBUD_DISTRIBUTOR_ID in .env. Found: {nodes}")
        self._distributor_id = nodes[0]["id"]
        return self._distributor_id

    def customer_locations(self):
        return self._paginate(
            "customerLocations", location_fields(self.distributor_id()),
            "List all retail store locations of this distributor to sync them into the CRM",
            where={"distributorId": {"equals": self.distributor_id()}},
        )

    def locations_by_search(self, text):
        """Find locations whose name, license number or customer name contains text."""
        return self._paginate(
            "customerLocations", "id name",
            "Find the store location that matches a license number already stored in the CRM",
            where={"distributorId": {"equals": self.distributor_id()}, "search": text},
            category="LOOKUP",
        )

    def products(self):
        return self._paginate("products", PRODUCT_FIELDS,
                              "List all products and their sellable options with price and stock for CRM sync")

    def orders_since(self, iso_date):
        return self._paginate(
            "orders", ORDER_FIELDS,
            "List recent orders with their line items and status to sync them into the CRM",
            where={"distributorId": {"equals": self.distributor_id()}, "createdAt": {"gte": iso_date}},
        )

    def orders_by_ids(self, ids):
        if not ids:
            return []
        return self._paginate(
            "orders", ORDER_FIELDS,
            "Re-check open orders that are already in the CRM for status or line changes",
            where={"id": {"in": list(ids)}},
        )

    # -- writes (Zoho -> Superbud) -------------------------------------------
    def update_customer_location(self, location_id, name=None, address=None):
        payload = {}
        if name:
            payload["name"] = name
        if address:
            payload["address"] = address
        return self.query("""
        mutation Update($id: ID!, $input: UpdateCustomerLocationInput!) {
          updateCustomerLocation(customerLocationId: $id, input: $input) { id name }
        }""", {"id": location_id, "input": payload})

    def type_fields(self, type_name):
        """Developer helper: list the real fields of a Superbud type (if introspection is on)."""
        data = self.query("query T($n: String!) { __type(name: $n) { name fields { name } inputFields { name } } }",
                          {"n": type_name})
        return data.get("__type")
