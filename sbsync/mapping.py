"""Business rules: how Superbud values turn into Zoho values."""
import json
import re

from .config import ROOT

# ---------------------------------------------------------------- orders -----
CLOSED_LOST = {"CANCELLED", "REJECTED", "MERGED"}
WON = {"CONFIRMED", "PACKED", "SHIPPED", "DELIVERED"}


def deal_stage(status, approval_state=None):
    if status in CLOSED_LOST:
        return "Closed Lost"
    if status in WON or approval_state == "APPROVED":
        return "Closed Won"
    return "open"


def sales_order_status(status):
    if status in CLOSED_LOST:
        return "Cancelled"
    if status in ("SHIPPED", "DELIVERED"):
        return "Delivered"
    if status in ("CONFIRMED", "PACKED"):
        return "Approved"
    return "Created"


def deal_name(order):
    location = (order.get("customerLocation") or {}).get("name") or (order.get("customer") or {}).get("name") or ""
    return f"SB-{order.get('orderNumber')} - {location}".strip(" -")


# ------------------------------------------------------------- addresses -----
def zoho_address(address, prefix):
    """Superbud Address -> Zoho Billing_/Shipping_ fields."""
    if not address:
        return {}
    street = ", ".join(p for p in (address.get("line1"), address.get("line2")) if p)
    values = {
        f"{prefix}_Street": street,
        f"{prefix}_City": address.get("city"),
        f"{prefix}_State": address.get("state"),
        f"{prefix}_Code": address.get("zip"),
        f"{prefix}_Country": address.get("country"),
    }
    return {k: v for k, v in values.items() if v}


# ---------------------------------------------------------- dispensaries -----
def dispensary_name(location, locations_per_customer):
    """Zoho Dispensary name = Superbud company name; add the store name when the
    company has more than one location (the license is not exposed by the API)."""
    company = (location.get("customer") or {}).get("name") or location.get("name") or "Unknown"
    customer_id = (location.get("customer") or {}).get("id")
    if locations_per_customer.get(customer_id, 1) > 1 and location.get("name") and location["name"] != company:
        return f"{company} - {location['name']}"
    return company


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# -------------------------------------------------------------- products -----
def load_product_map(path=ROOT / "product_map.json"):
    return json.loads(path.read_text(encoding="utf-8"))


def classify_product(product, product_map):
    """Work out Zoho Category / Sub Category / Flavour / name for a Superbud product."""
    name = product.get("name") or ""
    collections = [e["node"]["name"] for e in ((product.get("collections") or {}).get("edges") or [])]
    for rule in product_map:
        phrases = rule.get("strip", [])
        in_collection = any(norm(c) == norm(rule["collection"]) for c in collections)
        in_name = all(norm(p) in norm(name) for p in phrases) if phrases else False
        if in_collection or in_name:
            flavour = name
            for phrase in phrases:
                flavour = re.sub(re.escape(phrase), "", flavour, flags=re.IGNORECASE)
            flavour = re.sub(r"\s+", " ", flavour).strip(" -")
            # Superbud names like "50mg Gummies - Watermelon" already carry the prefix
            prefix_re = r"^" + re.escape(rule["name_prefix"]) + r"\s*-\s*"
            flavour = re.sub(prefix_re, "", flavour, flags=re.IGNORECASE).strip(" -")
            return {
                "Product_Category": rule["category"],
                "Sub_Category": rule["sub_category"],
                "Flavour": flavour or None,
                "display_name": f"{rule['name_prefix']} - {flavour}" if flavour else rule["name_prefix"],
            }
    return {"Product_Category": None, "Sub_Category": None, "Flavour": None, "display_name": name}


def product_key(sub_category, flavour):
    return (norm(sub_category), norm(flavour))


def unit_price(option, mode):
    price = option.get("basePriceDollars")
    if price is None:
        return None
    pack = option.get("packSize") or 1
    if mode == "unit" and pack:
        return round(price / pack, 4)
    return price
