"""Business rules: how Superbud values turn into Zoho values."""
import datetime as dt
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
    name = US_STATES.get(value.strip().upper(), value.strip())
    # Zoho's list has two places with these names, so the US ones carry a suffix
    return ZOHO_STATE_LABELS.get(name, name)


# State names that Zoho's picklist spells differently (checked against the live field list)
ZOHO_STATE_LABELS = {"Florida": "Florida (United States)", "Montana": "Montana (United States)"}


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


# ---------------------------------------------------------------- activity notes --
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

_NOISE_KEYS = {"id", "metadata", "__typename", "context", "userType", "source"}


def _label(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("id") or ""
    return "" if value is None else str(value)


def _when(iso):
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso or "")
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    t = t.astimezone(IST)
    return t.strftime("%d %b %Y, %I:%M %p IST").replace(" 0", " ").replace("AM", "am").replace("PM", "pm")


def _line_text(line):
    """One product line of a 'lines changed' event."""
    to, frm = line.get("to") or {}, line.get("from") or {}
    cur = to or frm
    meta = cur.get("metadata") or {}
    name = cur.get("name") or "Product"
    option = _label(meta.get("option"))
    qty = meta.get("quantity")
    old_qty = (frm.get("metadata") or {}).get("quantity")
    what = f"{name}" + (f" ({option})" if option else "")
    if to and not frm:
        return f"Added: {what}" + (f", qty {qty}" if qty is not None else "")
    if frm and not to:
        return f"Removed: {what}"
    if qty is not None and old_qty is not None and qty != old_qty:
        return f"Changed: {what}, qty {old_qty} → {qty}"
    return f"Changed: {what}" + (f", qty {qty}" if qty is not None else "")


def _plain(payload, depth=0):
    """Fallback: readable 'Key: value' lines for event types we don't format specially."""
    out = []
    for key, value in (payload or {}).items():
        if key in _NOISE_KEYS or value in (None, "", [], {}):
            continue
        nice = key[0].upper() + "".join(f" {c.lower()}" if c.isupper() else c for c in key[1:])
        if isinstance(value, dict) and ("name" in value or "id" in value):
            out.append(f"{nice}: {_label(value)}")
        elif isinstance(value, dict) and depth < 2:
            out.extend(_plain(value, depth + 1))
        elif isinstance(value, list):
            out.append(f"{nice}: " + ", ".join(_label(v) if isinstance(v, dict) else str(v) for v in value))
        else:
            out.append(f"{nice}: {value}")
    return out


def describe_activity(kind, event, occurred_at):
    """Superbud order history event -> (Zoho note title, plain-text body)."""
    event = event if isinstance(event, dict) else {}
    kind_text = str(kind or event.get("type") or "Activity").replace("_", " ").strip().title()
    actor = _label(event.get("actor")) or "Superbud"
    payload = event.get("payload") or {}
    when = _when(occurred_at) if occurred_at else ""
    by = f"By {actor}" + (f" on {when}" if when else "")
    key = kind_text.lower()
    if "status" in key and ("to" in payload or "from" in payload):
        frm, to = _label(payload.get("from")), _label(payload.get("to"))
        title = f"Status changed: {frm} → {to}" if frm else f"Status changed to {to}"
        return title, by
    if key in ("created", "order created"):
        return "Order created", by
    if "line" in key and isinstance(payload.get("lines"), list):
        lines = [_line_text(l) for l in payload["lines"] if isinstance(l, dict)]
        return "Products changed", "\n".join([by] + lines)
    detail = _plain(payload)
    return kind_text, "\n".join([by] + detail)


def zoho_qty(value):
    """Zoho's quantity fields reject negatives and long decimals. Superbud can show a
    negative available quantity when an item is oversold; Zoho gets 0 then."""
    if value is None:
        return None
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    qty = max(0.0, round(qty, 2))
    return int(qty) if qty.is_integer() else qty
