"""Dutchie products + stock + recent sales -> Zoho "Store Inventory", with a 3-week forecast.

One record per product per store, plus one per product for both stores together
(Earth's Healing "All Stores", "D2 - All Stores"; matched by SKU, else by product name). Each record carries the product
details from Dutchie (SKU, category, type, brand, vendor, strain, THC/CBD, size, price,
cost), stock on hand, units sold in the last 7/30/90 days, and a forecast:

  Avg Daily Units      weighted sales rate: 50% last 14 days, 30% last 28, 20% last 56
  Forecast 3 Weeks     Avg Daily Units x 21
  Days Of Stock        Qty On Hand / Avg Daily Units
  Projected Stockout   today + Days Of Stock (when within a year)
  Reorder Units        enough for 3 weeks + 1 week buffer, minus what is on hand
  Stock Status         Out of Stock / Runs Out Within 3 Weeks / Low (3-5 Weeks) /
                       Healthy / Overstock (120+ Days) / No Sales in 90 Days
"""
import datetime as dt
import math
from collections import defaultdict
from zoneinfo import ZoneInfo

from . import config
from .http import ApiError
from .retail import g, local_time, num, _same

MODULE = "Store_Inventory"
HISTORY_DAYS = 90
FORECAST_DAYS = 21
BUFFER_DAYS = 7

NUMERIC = ("Retail_Price", "Unit_Cost", "Stock_Value", "Retail_Value", "Sales_Last_30_Days",
           "Forecast_Revenue_3_Weeks", "Qty_On_Hand", "Qty_Allocated", "Units_Sold_7_Days", "Units_Sold_30_Days",
           "Units_Sold_90_Days", "Forecast_Units_3_Weeks", "Reorder_Units", "Days_Of_Stock", "Days_Since_Last_Sale",
           "Sales_Trend_Pct", "Avg_Daily_Units")
TEXT = ("Name", "Store", "Stock_Status", "Category", "Product_Type", "Brand", "Vendor", "SKU", "Dutchie_Product_ID",
        "Strain", "Strain_Type", "THC", "CBD", "Size", "Active_in_Dutchie", "Last_Sold_Date",
        "Projected_Stockout_Date", "Stock_As_Of")


def _content(p, key, unit_key):
    v = g(p, key)
    if v in (None, "", 0):
        return None
    unit = g(p, unit_key, default="") or ""
    unit = {"Percentage": "%", "Milligrams": "mg"}.get(unit, unit)
    return f"{v:g} {unit}".strip() if isinstance(v, (int, float)) else f"{v} {unit}".strip()


def product_details(p):
    """Common product fields from a Dutchie product (or inventory) row."""
    size = None
    if g(p, "unitWeight") not in (None, "", 0):
        size = f"{num(g(p, 'unitWeight')):g} {g(p, 'unitWeightUnit', default='') or ''}".strip()
    elif g(p, "netWeight") not in (None, "", 0):
        size = f"{num(g(p, 'netWeight')):g} {g(p, 'netWeightUnit', default='') or ''}".strip()
    active = g(p, "isActive")
    return {
        "Name": (g(p, "productName", "name", default="Unnamed product") or "")[:120],
        "Category": g(p, "masterCategory", "category", default=None) or "Uncategorized",
        "Product_Type": g(p, "category"),
        "Brand": g(p, "brandName", "brand"),
        "Vendor": g(p, "vendorName", "vendor", "producerName"),
        "SKU": g(p, "sku"),
        "Strain": g(p, "strain"),
        "Strain_Type": g(p, "strainType"),
        "THC": _content(p, "thcContent", "thcContentUnit"),
        "CBD": _content(p, "cbdContent", "cbdContentUnit"),
        "Size": size,
        "Active_in_Dutchie": active if isinstance(active, bool) else None,
        "Retail_Price": _money(g(p, "price", "recPrice", "unitPrice", "medPrice")),
    }


def _money(v):
    return round(num(v), 2) if v not in (None, "") else None


def daily_units(transactions, tz):
    """{productId: {date: (units, net $)}} from Dutchie sales (returns subtract, voids ignored)."""
    out = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for tx in transactions:
        if g(tx, "isVoid", default=False) is True or g(tx, "voidDate"):
            continue
        when = local_time(tx, tz)
        if when is None:
            continue
        sign = -1 if g(tx, "isReturn", default=False) else 1
        for it in g(tx, "items", default=[]) or []:
            pid = g(it, "productId")
            if pid is None:
                continue
            cell = out[pid][when.date()]
            cell[0] += sign * abs(num(g(it, "quantity")))
            cell[1] += sign * (abs(num(g(it, "totalPrice"))) - abs(num(g(it, "totalDiscount"))))
    return out


def forecast(qty, sales_by_day, today):
    """Sales history + stock -> forecast fields."""
    def units(a, b):
        """Units sold on the full days a+1 .. b days ago (today is not finished, so not counted)."""
        lo, hi = today - dt.timedelta(days=b), today - dt.timedelta(days=a)
        return sum(u for d, (u, _) in sales_by_day.items() if lo <= d < hi)

    u7, u14, u28, u30, u56, u90 = (units(0, n) for n in (7, 14, 28, 30, 56, 90))
    prev28 = units(28, 56)
    rate = max(0.0, 0.5 * u14 / 14 + 0.3 * u28 / 28 + 0.2 * u56 / 56)
    sold = [d for d, (u, _) in sales_by_day.items() if u > 0]
    last = max(sold) if sold else None
    f = {
        "Units_Sold_7_Days": round(u7), "Units_Sold_30_Days": round(u30), "Units_Sold_90_Days": round(u90),
        "Sales_Last_30_Days": round(sum(v for d, (_, v) in sales_by_day.items()
                                        if today - dt.timedelta(days=30) <= d < today), 2),
        "Avg_Daily_Units": round(rate, 2),
        "Forecast_Units_3_Weeks": math.ceil(rate * FORECAST_DAYS - 1e-9) if rate else 0,
        "Reorder_Units": max(0, math.ceil(rate * (FORECAST_DAYS + BUFFER_DAYS) - qty - 1e-9)) if rate else 0,
        "Last_Sold_Date": last.isoformat() if last else None,
        "Days_Since_Last_Sale": (today - last).days if last else None,
        "Sales_Trend_Pct": round((u28 - prev28) / prev28 * 100) if prev28 > 0 else None,
        "Days_Of_Stock": None, "Projected_Stockout_Date": None,
    }
    if rate > 0:
        days = qty / rate
        f["Days_Of_Stock"] = min(9999, math.floor(days))
        if days < 365:
            f["Projected_Stockout_Date"] = (today + dt.timedelta(days=math.floor(days))).isoformat()
    if qty <= 0:
        status = "Out of Stock"
    elif rate == 0:
        status = "No Sales in 90 Days"
    elif qty / rate < FORECAST_DAYS:
        status = "Runs Out Within 3 Weeks"
    elif qty / rate < 35:
        status = "Low (3-5 Weeks)"
    elif qty / rate > 120:
        status = "Overstock (120+ Days)"
    else:
        status = "Healthy"
    f["Stock_Status"] = status
    return f, rate


def store_rows(products, inventory, transactions, tz, today):
    """{product_id: record} for one store."""
    prod = {g(p, "productId", "id"): p for p in products}
    stock = defaultdict(lambda: {"qty": 0.0, "alloc": 0.0, "cost": 0.0, "row": None})
    for row in inventory:
        pid = g(row, "productId")
        s = stock[pid]
        q = num(g(row, "quantityAvailable", "quantity"))
        s["qty"] += q
        s["alloc"] += num(g(row, "allocatedQuantity"))
        s["cost"] += q * num(g(row, "unitCost"))
        s["row"] = s["row"] or row
    sales = daily_units(transactions, tz)
    rows = {}
    for pid in set(stock) | set(sales):
        if pid is None:
            continue
        p = prod.get(pid) or stock[pid]["row"] or {}
        rec = product_details(p)
        if rec["Retail_Price"] is None and stock[pid]["row"]:
            rec["Retail_Price"] = _money(g(stock[pid]["row"], "unitPrice"))
        qty = stock[pid]["qty"] if pid in stock else 0.0
        unit_cost = (stock[pid]["cost"] / qty) if qty > 0 else num(g(p, "unitCost"))
        f, rate = forecast(qty, sales.get(pid, {}), today)
        rec.update(f)
        rec.update({
            "Dutchie_Product_ID": str(pid),
            "Qty_On_Hand": round(qty), "Qty_Allocated": round(stock[pid]["alloc"]) if pid in stock else 0,
            "Unit_Cost": round(unit_cost, 2) if unit_cost else None,
            "Stock_Value": round(max(qty, 0) * unit_cost, 2),
            "Retail_Value": round(max(qty, 0) * (rec["Retail_Price"] or 0), 2),
            "Forecast_Revenue_3_Weeks": round(f["Forecast_Units_3_Weeks"] * (rec["Retail_Price"] or 0), 2),
            "Stock_As_Of": today.isoformat(),
        })
        if not qty and not f["Units_Sold_90_Days"]:
            continue  # nothing on hand and nothing sold recently: leave it out
        rows[pid] = (rec, sales.get(pid, {}))
    return rows


def combine(stores, today):
    """Both stores together, matched by SKU (else product name)."""
    groups = defaultdict(list)
    for rows in stores:
        for rec, sales in rows.values():
            key = (rec.get("SKU") or "").strip().lower() or "name:" + (rec.get("Name") or "").strip().lower()
            groups[key].append((rec, sales))
    out = {}
    for key, items in groups.items():
        base = dict(items[0][0])
        qty = sum(r["Qty_On_Hand"] for r, _ in items)
        sales = defaultdict(lambda: [0.0, 0.0])
        for _, s in items:
            for d, (u, v) in s.items():
                sales[d][0] += u
                sales[d][1] += v
        f, rate = forecast(qty, sales, today)
        base.update(f)
        stock_value = sum(r["Stock_Value"] for r, _ in items)
        base.update({
            "Dutchie_Product_ID": ", ".join(sorted({r["Dutchie_Product_ID"] for r, _ in items}))[:40],
            "Qty_On_Hand": qty, "Qty_Allocated": sum(r["Qty_Allocated"] for r, _ in items),
            "Stock_Value": round(stock_value, 2),
            "Retail_Value": round(sum(r["Retail_Value"] for r, _ in items), 2),
            "Unit_Cost": round(stock_value / qty, 2) if qty > 0 else base.get("Unit_Cost"),
            "Forecast_Revenue_3_Weeks": round(f["Forecast_Units_3_Weeks"] * (base.get("Retail_Price") or 0), 2),
        })
        out[key] = (base, sales)
    return out


class InventorySync:
    def __init__(self, zoho, report):
        self.z = zoho
        self.report = report

    def existing(self, store):
        cols = "id, Inventory_Key, " + ", ".join(NUMERIC + TEXT)
        s = store.replace("'", "''")
        return {r["Inventory_Key"]: r for r in self.z.coql_all(cols, MODULE, where=f"Store = '{s}'")
                if r.get("Inventory_Key")}

    def write(self, code, store, rows):
        have = self.existing(store)
        out = []
        for pid, (rec, _) in rows.items():
            key = f"{code}|{pid}"
            full = dict(rec, Store=store, Inventory_Key=key)
            old = have.pop(key, None)
            if old is not None:
                diff = {k: v for k, v in full.items()
                        if k not in ("Inventory_Key", "Name") and not _same(old.get(k), v)
                        and not (v is None and old.get(k) in (None, ""))}
                if not diff:
                    continue
                if old.get("Name") != full["Name"]:
                    diff["Name"] = full["Name"]
                out.append(dict(diff, id=old["id"]))
            else:
                out.append({k: v for k, v in full.items() if v is not None})
        today = rows and next(iter(rows.values()))[0]["Stock_As_Of"]
        for key, old in have.items():  # gone from Dutchie stock and no recent sales
            if old.get("Qty_On_Hand") or old.get("Stock_Status") != "Out of Stock":
                out.append({"id": old["id"], "Qty_On_Hand": 0, "Stock_Value": 0, "Retail_Value": 0,
                            "Stock_Status": "Out of Stock", "Stock_As_Of": today or None})
        creates = [r for r in out if "id" not in r]
        updates = [r for r in out if "id" in r]
        failed, codes = 0, set()
        for fn, recs in ((self.z.create, creates), (self.z.update, updates)):
            for i in range(0, len(recs), 100):
                for res in fn(MODULE, recs[i:i + 100]):
                    if res.get("status") not in ("success", "dry_run"):
                        failed += 1
                        codes.add(f"{res.get('code')} {(res.get('details') or {}).get('api_name') or ''}".strip())
        if failed:
            self.report.add("Inventory", "error", f"{store}: {failed} rows rejected", code=", ".join(sorted(codes))[:150])
        self.report.add("Inventory", "written" if creates or updates else "unchanged",
                        f"{store}: {len(creates)} new, {len(updates)} updated")

    def run(self, stores):
        """stores: [(code, label, Dutchie client)]"""
        per_store = []
        tz = ZoneInfo(config.get("RETAIL_TIMEZONE", "America/Phoenix"))
        for code, label, client in stores:
            try:
                try:
                    tz = ZoneInfo((client.whoami() or {}).get("timeZone") or str(tz))
                except Exception:
                    pass
                today = dt.datetime.now(tz).date()
                end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
                start = end - dt.timedelta(days=HISTORY_DAYS + 1)
                txs = []
                cur = start
                while cur < end:  # month-sized windows
                    nxt = min(cur + dt.timedelta(days=31), end)
                    txs.extend(client.transactions(cur, nxt))
                    cur = nxt
                rows = store_rows(client.products(), client.inventory(), txs, tz, today)
                self.write(code, label, rows)
                per_store.append((code, rows))
            except ApiError as exc:
                from .sync import safe_code
                self.report.add("Inventory", "error", f"{label}: {exc}", code=safe_code(exc))
        from .dutchie import GROUPS
        today = dt.datetime.now(tz).date()
        for gcode, glabel, members in GROUPS.values():  # e.g. Benson + River, Downtown + Eastside
            group = [rows for code, rows in per_store if code in members]
            if len(group) > 1:
                self.write(gcode, glabel, combine(group, today))
