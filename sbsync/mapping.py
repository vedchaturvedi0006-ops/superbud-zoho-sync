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
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
COUNTRIES = {"US": "United States", "USA": "United States", "U.S.": "United States",
             "UNITED STATES OF AMERICA": "United States", "CA": "Canada", "CAN": "Canada"}


def state_name(value):
    """Zoho's State dropdown uses full names ("Michigan"), Superbud sends codes ("MI")."""
    if not value:
        return value
    return US_STATES.get(value.strip().upper(), value.strip())


def country_name(value):
    if not value:
        return value
    return COUNTRIES.get(value.strip().upper(), value.strip())


def zoho_address(address, prefix, zip_field="Code"):
    """Superbud Address -> Zoho <prefix>_Street/_City/_State/_Code/_Country (+ apartment line)."""
    if not address:
        return {}
    values = {
        f"{prefix}_Street": address.get("line1"),
        f"{prefix}_Flat_House_No_Building_Apartment_Name": address.get("line2"),
        f"{prefix}_City": address.get("city"),
        f"{prefix}_State": state_name(address.get("state")),
        f"{prefix}_{zip_field}": address.get("zip"),
        f"{prefix}_Country": country_name(address.get("country")),
    }
    return {k: v for k, v in values.items() if v}


def payment_terms_text(terms):
    """Superbud payment-terms snapshot -> one readable line for Zoho's Terms and Conditions."""
    if not terms:
        return None
    parts = []
    if terms.get("paymentTerm"):
        parts.append(f"Payment term: {str(terms['paymentTerm']).replace('_', ' ').title()}")
    if terms.get("billOn"):
        parts.append(f"Bill on: {str(terms['billOn']).replace('_', ' ').title()}")
    if terms.get("delinquentAfterDays") is not None:
        parts.append(f"Delinquent after {terms['delinquentAfterDays']} days")
    if terms.get("preferredPaymentMethod"):
        parts.append(f"Preferred method: {str(terms['preferredPaymentMethod']).replace('_', ' ').title()}")
    return " | ".join(parts) or None


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
