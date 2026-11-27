"""Dutchie POS (Earth's Healing stores) -> Zoho CRM sales statistics for dashboards.

Zoho CRM can't hold every receipt, so each run totals the Dutchie sales and writes
summary rows into one custom module, "Retail Sales Stats". One row = one number set for
one store + one slice:

  Daily Total   per store per day       net/gross sales, discounts, returns, tax, sales count,
                                        avg basket, units, customers, new customers,
                                        medical vs rec, in-store / pickup / delivery
  Category      per store per month     per product category
  Product       per store per month     per product (with brand + category)
  Budtender     per store per month     per employee who rang up the sale
  Hour          per store per month     per weekday + hour of day
  Inventory     per store, today        stock on hand, stock value, days since last sale

Rows are matched by Stat_Key, so re-running is safe and only changed rows are written.
"""
import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

from . import config
from .http import ApiError

MODULE = "Retail_Sales_Stats"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

MONEY_FIELDS = ("Net_Sales", "Gross_Sales", "Discounts", "Returns", "Tax", "Cost", "Margin", "Avg_Basket",
                "Medical_Sales", "Rec_Sales", "Inventory_Value", "Prev_3M_Net_Sales", "Prev_3M_Avg_Basket")
COUNT_FIELDS = ("Transactions", "Customers", "New_Customers", "In_Store_Sales", "Pickup_Sales",
                "Delivery_Sales", "Days_Since_Last_Sale", "Days_In_Period", "Prev_3M_Transactions",
                "Net_Sales_vs_3M_Pct", "Orders_vs_3M_Pct", "Avg_Basket_vs_3M_Pct",
                "Net_Sales_1_Month_Ago", "Net_Sales_2_Months_Ago", "Net_Sales_3_Months_Ago",
                "Orders_1_Month_Ago", "Orders_2_Months_Ago", "Orders_3_Months_Ago", "Projected_Orders")
# Month Total comparison with each of the previous 3 full months, and the month-end projection.
# Zoho Professional allows only a few currency/decimal fields, so some older fields were relabelled
# in Zoho and reused (api name -> label):
#   Prev_3M_Avg_Basket   -> "AOV 1 Month Ago"      Prev_3M_Net_Sales -> "AOV 2 Months Ago"
#   Inventory_Value      -> "AOV 3 Months Ago"     Prev_3M_Transactions -> "Projected Net Sales" (whole $)
NET_AGO = ("Net_Sales_1_Month_Ago", "Net_Sales_2_Months_Ago", "Net_Sales_3_Months_Ago")  # whole $
ORDERS_AGO = ("Orders_1_Month_Ago", "Orders_2_Months_Ago", "Orders_3_Months_Ago")
AOV_AGO = ("Prev_3M_Avg_Basket", "Prev_3M_Net_Sales", "Inventory_Value")
PROJECTED_NET = "Prev_3M_Transactions"
PROJECTED_ORDERS = "Projected_Orders"
RETIRED = ("Net_Sales_vs_3M_Pct", "Orders_vs_3M_Pct", "Avg_Basket_vs_3M_Pct")
DECIMAL_FIELDS = ("Units", "Qty_On_Hand")
ALL_STORES = "All Stores"
# daily fields that add up into a month
SUMMABLE = ("Net_Sales", "Gross_Sales", "Discounts", "Returns", "Tax", "Cost", "Margin", "Medical_Sales",
            "Rec_Sales", "Transactions", "New_Customers", "In_Store_Sales", "Pickup_Sales", "Delivery_Sales",
            "Units")


# ------------------------------------------------------------------ helpers --
def g(obj, *names, default=None):
    """First present value among several possible Dutchie field names."""
    if not isinstance(obj, dict):
        return default
    for n in names:
        if obj.get(n) not in (None, ""):
            return obj[n]
    lower = {k.lower(): v for k, v in obj.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return v
    return default


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_time(value):
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t


def local_time(tx, tz):
    """Store-local time of a sale. Dutchie gives UTC (and sometimes a local copy)."""
    local = parse_time(g(tx, "transactionDateLocalTime", "transactionDateLocal"))
    if local is not None:
        return local.replace(tzinfo=None) if local.tzinfo is None else local.astimezone(tz).replace(tzinfo=None)
    t = parse_time(g(tx, "transactionDate", "transactionDateUTC", "checkInDate"))
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(tz).replace(tzinfo=None)


def channel(tx):
    text = " ".join(str(g(tx, k, default="")) for k in ("orderType", "orderSource", "orderMethod")).lower()
    if "deliver" in text:
        return "Delivery_Sales"
    if "pick" in text or "online" in text or "pre" in text or bool(g(tx, "wasPreOrdered", default=False)):
        return "Pickup_Sales"
    return "In_Store_Sales"


def is_medical(tx, customers):
    flag = g(tx, "isMedical", "medical")
    if isinstance(flag, bool):
        return flag
    ctype = str(g(tx, "customerType", "customerTypeName", default="") or "").lower()
    if not ctype:
        cust = customers.get(g(tx, "customerId")) or {}
        ctype = str(g(cust, "customerType", "customerTypeName", default="") or "").lower()
        if not ctype and isinstance(g(cust, "isMedical"), bool):
            return cust["isMedical"]
    return "med" in ctype


# --------------------------------------------------------------- aggregation --
class Totals:
    __slots__ = ("net", "gross", "disc", "ret", "tax", "cost", "units", "tx", "cust", "med", "rec", "chan", "new")

    def __init__(self):
        self.net = self.gross = self.disc = self.ret = self.tax = self.cost = self.units = 0.0
        self.med = self.rec = 0.0
        self.tx = set()
        self.cust = set()
        self.new = set()
        self.chan = defaultdict(set)

    def fields(self):
        out = {
            "Net_Sales": round(self.net, 2), "Gross_Sales": round(self.gross, 2), "Discounts": round(self.disc, 2),
            "Returns": round(self.ret, 2), "Tax": round(self.tax, 2), "Units": round(self.units, 2),
            "Transactions": len(self.tx), "Customers": len(self.cust),
            "Avg_Basket": round(self.net / len(self.tx), 2) if self.tx else 0,
        }
        if self.cost:
            out["Cost"] = round(self.cost, 2)
            out["Margin"] = round(self.net - self.cost, 2)
        return out


def aggregate(transactions, products, employees, customers, tz, first_purchase=None):
    """Dutchie sales -> {stat_key_suffix: (stat_type, period_start, dimension, extra, fields)}."""
    prod_by_id = {g(p, "productId", "id"): p for p in products}
    emp_by_id = {g(e, "userId", "employeeId", "id"): e for e in employees}
    first_purchase = first_purchase or {}

    daily = defaultdict(Totals)
    category = defaultdict(Totals)
    product = defaultdict(Totals)
    staff = defaultdict(Totals)
    hourly = defaultdict(Totals)
    product_info = {}
    last_sold = {}

    for tx in transactions:
        if g(tx, "isVoid", default=False) is True or g(tx, "voidDate"):
            continue
        when = local_time(tx, tz)
        if when is None:
            continue
        day = when.date()
        month = day.replace(day=1)
        tid = g(tx, "transactionId", "id")
        cid = g(tx, "customerId")
        returning = bool(g(tx, "isReturn", default=False))
        sign = -1 if returning else 1
        emp = emp_by_id.get(g(tx, "employeeId", "completedByUserId", "responsibleForSaleUserId")) or {}
        emp_name = g(emp, "fullName", "name", "loginId", default=None) or \
            g(tx, "employeeName", "completedByUser", default="Unknown")
        medical = is_medical(tx, customers)
        chan = channel(tx)
        hour_key = (month, f"{WEEKDAYS[when.weekday()]} {when.hour:02d}:00", when.weekday(), when.hour)

        tx_net = 0.0
        items = g(tx, "items", default=[]) or []
        for it in items:
            if g(it, "isReturned", default=False) is True and not returning:
                # line refunded later: the refund shows up as its own return transaction
                pass
            qty = num(g(it, "quantity"))
            gross = num(g(it, "totalPrice"))
            disc = num(g(it, "totalDiscount"))
            cost = num(g(it, "unitCost")) * qty
            if returning:
                gross, disc, cost, qty = -abs(gross), -abs(disc), -abs(cost), -abs(qty)
            net = gross - disc
            tx_net += net
            pid = g(it, "productId")
            p = prod_by_id.get(pid) or {}
            cat = g(p, "masterCategory", "category", default=None) or g(it, "category", default="Uncategorized")
            name = g(p, "productName", "name", default=None) or g(it, "productName", default=f"Product {pid}")
            product_info[pid] = (name, g(p, "brandName", "brand", default=None) or g(it, "brandName"), cat,
                                 g(p, "category", default=None))
            if not returning:
                last_sold[pid] = max(last_sold.get(pid, day), day)
            for bucket in (category[(month, cat)], product[(month, pid)]):
                bucket.net += net
                bucket.gross += gross
                bucket.disc += disc
                bucket.cost += cost
                bucket.units += qty
                bucket.tx.add(tid)
                if cid:
                    bucket.cust.add(cid)
            for bucket in (daily[day], staff[(month, emp_name)], hourly[hour_key]):
                bucket.units += qty
                bucket.cost += cost

        if not items:  # no line detail: fall back to the transaction totals
            tx_net = sign * (abs(num(g(tx, "subtotal"))) - abs(num(g(tx, "totalDiscount"))))
        tx_gross = sign * abs(num(g(tx, "subtotal", default=tx_net)))
        tx_disc = sign * abs(num(g(tx, "totalDiscount")))
        tx_tax = sign * abs(num(g(tx, "tax", "totalTax")))
        for bucket in (daily[day], staff[(month, emp_name)], hourly[hour_key]):
            bucket.net += tx_net
            bucket.gross += tx_gross
            bucket.disc += tx_disc
            bucket.tax += tx_tax
            if returning:
                bucket.ret += abs(tx_net)
            else:
                bucket.tx.add(tid)
                if cid:
                    bucket.cust.add(cid)
        d = daily[day]
        if medical:
            d.med += tx_net
        else:
            d.rec += tx_net
        if not returning:
            d.chan[chan].add(tid)
            if cid and first_purchase.get(cid) == day:
                d.new.add(cid)

    rows = {}
    for day, t in daily.items():
        f = t.fields()
        f.update({"Medical_Sales": round(t.med, 2), "Rec_Sales": round(t.rec, 2), "New_Customers": len(t.new),
                  "In_Store_Sales": len(t.chan["In_Store_Sales"]), "Pickup_Sales": len(t.chan["Pickup_Sales"]),
                  "Delivery_Sales": len(t.chan["Delivery_Sales"])})
        rows[f"Daily Total|{day}"] = ("Daily Total", day, "All sales", {}, f)
    for (month, cat), t in category.items():
        rows[f"Category|{month}|{cat}"] = ("Category", month, cat, {"Category": cat}, t.fields())
    for (month, pid), t in product.items():
        name, brand, cat, ptype = product_info.get(pid, (f"Product {pid}", None, None, None))
        extra = {"Category": cat, "Product_Type": ptype, "Brand": brand, "Dutchie_Product_ID": str(pid)}
        rows[f"Product|{month}|{pid}"] = ("Product", month, name, extra, t.fields())
    for (month, emp), t in staff.items():
        rows[f"Budtender|{month}|{emp}"] = ("Budtender", month, emp, {}, t.fields())
    for (month, label, wd, hour), t in hourly.items():
        rows[f"Hour|{month}|{label}"] = ("Hour", month, label, {"Weekday": WEEKDAYS[wd], "Hour_of_Day": hour},
                                         t.fields())
    return rows, last_sold


def inventory_rows(inventory, products, today, last_sold):
    prod_by_id = {g(p, "productId", "id"): p for p in products}
    qty = defaultdict(float)
    value = defaultdict(float)
    for row in inventory:
        pid = g(row, "productId")
        prod_by_id.setdefault(pid, row)  # inventory rows carry name/category/brand too
        q = num(g(row, "quantityAvailable", "quantity", "availableQuantity"))
        qty[pid] += q
        value[pid] += q * num(g(row, "unitCost", "cost"))
    rows = {}
    for pid, q in qty.items():
        p = prod_by_id.get(pid) or {}
        name = g(p, "productName", "name", default=None) or f"Product {pid}"
        cat = g(p, "masterCategory", "category", default=None)
        last = last_sold.get(pid)
        f = {"Qty_On_Hand": round(q, 2), "Inventory_Value": None,  # field now holds Month Total AOV
             "Days_Since_Last_Sale": (today - last).days if last else None}
        extra = {"Category": cat, "Product_Type": g(p, "category"), "Brand": g(p, "brandName", "brand"),
                 "Dutchie_Product_ID": str(pid)}
        rows[f"Inventory|{pid}"] = ("Inventory", today, name, extra, f)
    return rows


def month_ranges(start, end, tz):
    """Whole store-local calendar months covering start..end:
    [(utc_from, utc_to, first_day, last_day)]. Months must follow the store's clock,
    otherwise a month's last evening lands in the next window and overwrites it."""
    out = []
    s = start.astimezone(tz)
    cur = dt.datetime(s.year, s.month, 1, tzinfo=tz)
    while cur < end:
        nxt = dt.datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=tz)
        out.append((cur.astimezone(dt.timezone.utc), min(nxt, end).astimezone(dt.timezone.utc),
                    cur.date(), (nxt - dt.timedelta(days=1)).date()))
        cur = nxt
    return out


# ---------------------------------------------------------------------- sync --
class RetailSync:
    """Writes the totals for one store into Zoho. Only changed rows are sent."""

    def __init__(self, zoho, report):
        self.z = zoho
        self.report = report
        self.tz = ZoneInfo(config.get("RETAIL_TIMEZONE", "America/Phoenix"))

    def existing(self, store_label, since, until, with_inventory=False, stat_type=None):
        store = store_label.replace("'", "''")  # COQL escapes a quote by doubling it
        if with_inventory:
            where = f"(Store = '{store}') and (Stat_Type = 'Inventory')"
        elif stat_type:
            where = (f"((Store = '{store}') and (Stat_Type = '{stat_type}')) and "
                     f"(Period_Start between '{since:%Y-%m-%d}' and '{until:%Y-%m-%d}')")
        else:
            where = (f"((Store = '{store}') and (Stat_Type not in ('Inventory', 'Month Total'))) and "
                     f"(Period_Start between '{since:%Y-%m-%d}' and '{until:%Y-%m-%d}')")
        cols = "id, Stat_Key, Period_Start, Dutchie_Product_ID, " + ", ".join(MONEY_FIELDS + COUNT_FIELDS + DECIMAL_FIELDS)
        return {r["Stat_Key"]: r for r in self.z.coql_all(cols, MODULE, where=where) if r.get("Stat_Key")}

    def write(self, store_code, store_label, rows, have):
        """Create/update rows; rows already in Zoho for this period that have no sales now are zeroed."""
        out = []
        for suffix, (stype, period, dim, extra, fields) in rows.items():
            key = f"{store_code}|{suffix}"
            rec = {"Name": f"{store_label.split(' - ')[-1]} | {stype} | {period:%Y-%m-%d} | {dim}"[:120],
                   "Stat_Key": key, "Store": store_label, "Stat_Type": stype,
                   "Period_Start": period.strftime("%Y-%m-%d"), "Dimension": str(dim)[:250]}
            rec.update({k: v for k, v in extra.items() if v not in (None, "")})
            rec.update({k: v for k, v in fields.items() if v is not None})
            old = have.pop(key, None)
            if old is not None:
                # a value that became blank (e.g. no comparison baseline any more) is cleared in Zoho
                cleared = {k: None for k, v in fields.items() if v is None and old.get(k) not in (None, "")}
                if not cleared and all(_same(old.get(k), v) for k, v in fields.items() if v is not None):
                    continue
                rec.update(cleared)
                rec["id"] = old["id"]
            out.append(rec)
        for key, old in have.items():
            zero = {k: 0 for k in MONEY_FIELDS + COUNT_FIELDS + DECIMAL_FIELDS if old.get(k)}
            if zero:
                out.append(dict(zero, id=old["id"]))
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
            self.report.add("Retail stats", "error", f"{store_label}: {failed} rows rejected",
                            code=", ".join(sorted(codes))[:150])
        return len(creates), len(updates)

    def sync_store(self, code, label, client, start, end, seen=None):
        """Refresh all statistics for one store, month by month, from `start` to `end` (UTC).

        `seen` = customers who bought before `start` (full backfill passes it along so
        'new customers' means first purchase ever). On regular runs Dutchie's customer
        sign-up date is used instead."""
        try:
            self.tz = ZoneInfo((client.whoami() or {}).get("timeZone") or config.get("RETAIL_TIMEZONE", "America/Phoenix"))
        except Exception:  # unknown zone name: keep the default
            self.tz = ZoneInfo(config.get("RETAIL_TIMEZONE", "America/Phoenix"))
        products = client.products()
        try:
            employees = client.employees()
        except ApiError:
            employees = []  # this key has no Employee scope: budtenders show as IDs
        signups = {} if seen is not None else self.signups(client, start)
        new_total = upd_total = 0
        last_sold = {}
        for a, b, la, lb in month_ranges(start, end, self.tz):
            txs = client.transactions(a, b)
            if seen is not None:
                first = {}
                for tx in sorted(txs, key=lambda t: str(g(t, "transactionDate", default=""))):
                    cid = g(tx, "customerId")
                    when = local_time(tx, self.tz)
                    if cid and when and cid not in seen and not g(tx, "isReturn", default=False):
                        seen.add(cid)
                        first[cid] = when.date()
            else:
                first = signups
            rows, sold = aggregate(txs, products, employees, {}, self.tz, first)
            for pid, d in sold.items():
                last_sold[pid] = max(last_sold.get(pid, d), d)
            have = self.existing(label, la, lb)
            c, u = self.write(code, label, rows, have)
            new_total += c
            upd_total += u
        try:
            today = dt.datetime.now(self.tz).date()
            have_inv = self.existing(label, None, None, with_inventory=True)
            # products not sold in this run's window: carry the last-sale date over from Zoho
            for old in have_inv.values():
                pid, days, when = old.get("Dutchie_Product_ID"), old.get("Days_Since_Last_Sale"), old.get("Period_Start")
                if pid and days not in (None, "") and when:
                    prev = dt.date.fromisoformat(str(when)[:10]) - dt.timedelta(days=int(days))
                    for key in (pid, _int_or_none(pid)):
                        if key is not None and key not in last_sold:
                            last_sold[key] = prev
            inv = inventory_rows(client.inventory(), products, today, last_sold)
            c, u = self.write(code, label, inv, have_inv)
            new_total += c
            upd_total += u
        except ApiError as exc:
            self.report.add("Retail stats", "error", f"{label}: inventory {exc}", code="INVENTORY")
        outcome = "written" if new_total or upd_total else "unchanged"
        self.report.add("Retail stats", outcome, f"{label}: {new_total} new, {upd_total} updated rows")
        return new_total, upd_total

    # -------------------------------------------------------------- month totals --
    def daily_rows(self, since, until):
        """Daily Total rows of every store from Zoho: {(store, date): fields}."""
        cols = "Store, Period_Start, " + ", ".join(SUMMABLE)
        where = f"(Stat_Type = 'Daily Total') and (Period_Start between '{since:%Y-%m-%d}' and '{until:%Y-%m-%d}')"
        out = {}
        for r in self.z.coql_all(cols, MODULE, where=where):
            if r.get("Store") and r.get("Period_Start"):
                out[(r["Store"], dt.date.fromisoformat(str(r["Period_Start"])[:10]))] = r
        return out

    def month_totals(self, store_codes, first_month, today):
        """One 'Month Total' row per store (and for all stores together) per month, with the exact
        average order value and a comparison with the previous 3 months. The current month is
        compared over the same days (e.g. 1-25 Nov vs 1-25 of Aug, Sep, Oct)."""
        first_month = first_month.replace(day=1)
        since = _add_months(first_month, -3)
        daily = self.daily_rows(since, today)
        from .dutchie import ARIZONA, GROUPS, group_of
        codes = dict(store_codes)
        totals_of = {}  # store label -> combined labels it adds into
        for label, code in store_codes.items():
            grp = group_of(code)
            if grp:
                gcode, glabel, members = GROUPS[grp]
                if sum(1 for c in store_codes.values() if c in members) > 1:
                    codes[glabel] = gcode
                    totals_of.setdefault(label, []).append(glabel)
            if len(store_codes) > 1 and len({group_of(c) for c in store_codes.values()}) > 1:
                codes[ARIZONA[1]] = ARIZONA[0]
                totals_of.setdefault(label, []).append(ARIZONA[1])
        by_store = defaultdict(dict)
        for (store, day), r in daily.items():
            if store not in store_codes:
                continue  # combined rows are rebuilt from the single stores
            by_store[store][day] = r
            for combined in totals_of.get(store, []):
                tot = by_store[combined].setdefault(day, {})
                for f in SUMMABLE:
                    tot[f] = num(tot.get(f)) + num(r.get(f))
        new_total = upd_total = 0
        for store, days in by_store.items():
            code = codes.get(store)
            if not code:
                continue
            rows = {}
            month = first_month
            while month <= today:
                last_day = None
                if (month.year, month.month) == (today.year, today.month):
                    # month to date: up to the last day that has sales (today may have none yet)
                    seen = [d.day for d in days if d.year == month.year and d.month == month.month]
                    last_day = max(seen) if seen else today.day
                cur = _month_sum(days, month, last_day)
                if cur["_days"]:
                    f = {k: (int(round(cur[k])) if k in COUNT_FIELDS else round(cur[k], 2)) for k in SUMMABLE}
                    f["Avg_Basket"] = round(cur["Net_Sales"] / cur["Transactions"], 2) if cur["Transactions"] else 0
                    f["Days_In_Period"] = cur["_days"]
                    f.update({k: None for k in RETIRED})
                    # each of the previous 3 months in full (left blank when that month is
                    # missing or only partly in the history, e.g. the first month of data)
                    for k in (1, 2, 3):
                        pm = _add_months(month, -k)
                        p = _month_sum(days, pm)
                        full = p["_days"] >= 0.9 * _days_in_month(pm)
                        f[NET_AGO[k - 1]] = int(round(p["Net_Sales"])) if full else None
                        f[ORDERS_AGO[k - 1]] = int(round(p["Transactions"])) if full else None
                        f[AOV_AGO[k - 1]] = (round(p["Net_Sales"] / p["Transactions"], 2)
                                             if full and p["Transactions"] else None)
                    # month-end projection at this month's daily pace (a finished month is its actual)
                    base, elapsed = cur, last_day
                    if last_day:
                        # pace over the calendar days already finished (a closed day such as a
                        # holiday still counts as passed); today is still trading so it is left out
                        if today.day > 1:
                            base, elapsed = _month_sum(days, month, today.day - 1), today.day - 1
                        else:
                            elapsed = 1
                    scale = _days_in_month(month) / elapsed if elapsed else 1
                    f[PROJECTED_NET] = int(round(base["Net_Sales"] * scale))
                    f[PROJECTED_ORDERS] = int(round(base["Transactions"] * scale))
                    label = "month to date" if last_day else "full month"
                    rows[f"Month Total|{month}"] = ("Month Total", month, f"{month:%b %Y} ({label})", {}, f)
                month = _add_months(month, 1)
            have = self.existing(store, first_month, today, stat_type="Month Total")
            c, u = self.write(code, store, rows, have)
            new_total += c
            upd_total += u
        self.report.add("Retail stats", "written" if new_total or upd_total else "unchanged",
                        f"month totals: {new_total} new, {upd_total} updated rows")

    def signups(self, client, since):
        """Customer id -> local sign-up day, for customers changed since `since`."""
        try:
            rows = client.customers(since)
        except ApiError:
            return {}
        out = {}
        for c in rows:
            created = parse_time(g(c, "creationDate", "createdDate", "dateCreated"))
            cid = g(c, "customerId", "id")
            if cid and created:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=dt.timezone.utc)
                out[cid] = created.astimezone(self.tz).date()
        return out


def _add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def _days_in_month(month):
    return (_add_months(month, 1) - dt.timedelta(days=1)).day


def _month_sum(days, month, last_day=None):
    """Totals of one month's daily rows (only days 1..last_day when given)."""
    out = {k: 0.0 for k in SUMMABLE}
    out["_days"] = 0
    for day, r in days.items():
        if day.year == month.year and day.month == month.month and (last_day is None or day.day <= last_day):
            out["_days"] += 1
            for k in SUMMABLE:
                out[k] += num(r.get(k))
    return out


def _pct(cur, base):
    """Whole-number % change (Zoho Professional allows only a few decimal fields)."""
    return round((cur - base) / base * 100) if base else None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _same(a, b):
    if a in (None, "") and b in (0, None, ""):
        return True
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return str(a) == str(b)
