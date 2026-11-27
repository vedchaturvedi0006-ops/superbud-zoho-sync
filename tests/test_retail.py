import datetime as dt
import os
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sbsync import retail  # noqa: E402
from sbsync.sync import Report  # noqa: E402

TZ = ZoneInfo("America/Phoenix")


def tx(tid, when, items, cust="C1", emp=7, **kw):
    base = {"transactionId": tid, "transactionDate": when, "customerId": cust, "employeeId": emp,
            "isVoid": False, "isReturn": False, "isMedical": False, "orderType": "In Store",
            "subtotal": sum(i["totalPrice"] for i in items), "totalDiscount": sum(i["totalDiscount"] for i in items),
            "tax": 2.0, "items": items}
    base.update(kw)
    return base


def item(pid, qty, price, disc=0.0, cost=1.0):
    return {"productId": pid, "quantity": qty, "totalPrice": price, "totalDiscount": disc, "unitCost": cost}


PRODUCTS = [{"productId": 1, "productName": "Blue Dream 3.5g", "masterCategory": "Flower", "category": "Eighths",
             "brandName": "House"},
            {"productId": 2, "productName": "Mango Gummies", "masterCategory": "Edibles", "category": "Gummies",
             "brandName": "Chew"}]
EMPS = [{"userId": 7, "fullName": "Alex Budtender"}]


class FakeZoho:
    def __init__(self):
        self.rows = {}
        self.calls = 0

    def coql_all(self, cols, module, where=""):
        import re
        m = re.search(r"between '([\d-]+)' and '([\d-]+)'", where)
        st = re.search(r"Stat_Type = '([^']+)'", where)
        store = re.search(r"Store = '((?:[^']|'')+)'", where)
        out = []
        for r in self.rows.values():
            if st and r["Stat_Type"] != st.group(1):
                continue
            if not st and r["Stat_Type"] in ("Inventory", "Month Total", "Month Compare"):
                continue
            if store and r.get("Store") != store.group(1).replace("''", "'"):
                continue
            if m and not (m.group(1) <= r["Period_Start"] <= m.group(2)):
                continue
            out.append(dict(r))
        return out

    def create(self, module, recs):
        self.calls += 1
        out = []
        for r in recs:
            rid = str(len(self.rows) + 1)
            self.rows[rid] = dict(r, id=rid)
            out.append({"status": "success", "details": {"id": rid}})
        return out

    def update(self, module, recs):
        self.calls += 1
        for r in recs:
            self.rows[r["id"]].update(r)
        return [{"status": "success"} for _ in recs]

    def delete(self, module, ids):
        for i in ids:
            self.rows.pop(i, None)
        return [{"status": "success"} for _ in ids]


class FakeDutchie:
    def __init__(self, txs):
        self.txs = txs

    def whoami(self):
        return {"timeZone": "America/Phoenix"}

    def products(self):
        return PRODUCTS

    def employees(self):
        return EMPS

    def customers(self, since):
        return [{"customerId": "C2", "creationDate": "2026-11-10T18:00:00Z"}]

    def inventory(self):
        return [{"productId": 1, "quantityAvailable": 12, "unitCost": 10}]

    def transactions(self, a, b):
        return [t for t in self.txs if a <= dt.datetime.fromisoformat(t["transactionDate"].replace("Z", "+00:00")) < b]


SALES = [
    tx(1, "2026-11-10T17:00:00Z", [item(1, 2, 60.0, 6.0, 10), item(2, 1, 20.0)]),
    tx(2, "2026-11-10T19:30:00Z", [item(2, 3, 60.0)], cust="C2", isMedical=True, orderType="Delivery"),
    tx(3, "2026-11-10T20:00:00Z", [item(1, 1, 30.0)], isVoid=True),
    tx(4, "2026-11-11T18:00:00Z", [item(2, 1, 20.0)], isReturn=True),
]


class RetailTests(unittest.TestCase):
    def test_daily_totals(self):
        rows, last = retail.aggregate(SALES, PRODUCTS, EMPS, {}, TZ, {"C2": dt.date(2026, 11, 10)})
        d = rows["Daily Total|2026-11-10"][4]
        self.assertEqual(d["Net_Sales"], 134.0)       # (60-6)+20+60, void ignored
        self.assertEqual(d["Transactions"], 2)
        self.assertEqual(d["Avg_Basket"], 67.0)
        self.assertEqual(d["Medical_Sales"], 60.0)
        self.assertEqual(d["Delivery_Sales"], 1)
        self.assertEqual(d["New_Customers"], 1)
        r = rows["Daily Total|2026-11-11"][4]
        self.assertEqual((r["Net_Sales"], r["Returns"], r["Transactions"]), (-20.0, 20.0, 0))
        cat = rows["Category|2026-11-01|Edibles"][4]
        self.assertEqual((cat["Net_Sales"], cat["Units"]), (60.0, 3.0))  # 20 + 60 - 20 returned
        prod = rows["Product|2026-11-01|1"]
        self.assertEqual(prod[2], "Blue Dream 3.5g")
        self.assertEqual(prod[3]["Product_Type"], "Eighths")
        self.assertIn("Budtender|2026-11-01|Alex Budtender", rows)
        self.assertIn("Hour|2026-11-01|Tue 10:00", rows)  # 17:00Z = 10:00 Phoenix

    def test_sync_writes_once_then_nothing(self):
        z, rep = FakeZoho(), Report()
        rs = retail.RetailSync(z, rep)
        start = dt.datetime(2026, 11, 1, 7, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
        new, upd = rs.sync_store("benson", "Earth's Healing - Benson", FakeDutchie(SALES), start, end)
        self.assertGreater(new, 5)
        inv = [r for r in z.rows.values() if r["Stat_Type"] == "Inventory"][0]
        self.assertEqual((inv["Qty_On_Hand"], inv.get("Inventory_Value"), inv["Days_Since_Last_Sale"] is not None),
                         (12.0, None, True))  # Inventory_Value now holds Month Total AOV
        calls = z.calls
        new, upd = rs.sync_store("benson", "Earth's Healing - Benson", FakeDutchie(SALES), start, end)
        self.assertEqual((new, upd), (0, 0))
        self.assertEqual(z.calls, calls)


class SlowMoverTests(unittest.TestCase):
    def test_days_since_last_sale_keeps_counting(self):
        z, rep = FakeZoho(), Report()
        rs = retail.RetailSync(z, rep)
        z.rows["1"] = {"id": "1", "Stat_Key": "benson|Inventory|1", "Stat_Type": "Inventory", "Store": "B",
                       "Period_Start": "2026-11-01", "Dutchie_Product_ID": "1", "Days_Since_Last_Sale": 40,
                       "Qty_On_Hand": 12, "Inventory_Value": 120}
        # this month: product 1 not sold at all
        rs.sync_store("benson", "B", FakeDutchie([]), dt.datetime(2026, 11, 1, 7, tzinfo=dt.timezone.utc),
                      dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc))
        today = dt.datetime.now(rs.tz).date()
        expected = (today - (dt.date(2026, 11, 1) - dt.timedelta(days=40))).days
        self.assertEqual(z.rows["1"]["Days_Since_Last_Sale"], expected)


class MonthTotalTests(unittest.TestCase):
    def test_exact_aov_previous_months_and_projection(self):
        z, rep = FakeZoho(), Report()
        rs = retail.RetailSync(z, rep)

        def day(store, d, net, tx):
            k = f"{store}|{d}"
            z.rows[k] = {"id": k, "Stat_Key": k, "Store": store, "Stat_Type": "Daily Total", "Period_Start": d,
                         "Net_Sales": net, "Transactions": tx}
        for m, net in (("08", 100), ("09", 200), ("10", 300)):
            for dd in range(1, 32 if m != "09" else 31):
                day("Earth's Healing - Benson", f"2026-{m}-{dd:02d}", net, 2)
        for dd in range(1, 11):   # Nov 1-10 finished, today (11th) has a partial day
            day("Earth's Healing - Benson", f"2026-11-{dd:02d}", 150, 2)
            day("Earth's Healing - River", f"2026-11-{dd:02d}", 50, 1)
        day("Earth's Healing - Benson", "2026-11-11", 15, 1)
        rs.month_totals({"Earth's Healing - Benson": "benson", "Earth's Healing - River": "river"},
                        dt.date(2026, 10, 1), dt.date(2026, 11, 11))
        mt = {r["Stat_Key"]: r for r in z.rows.values() if r.get("Stat_Type") == "Month Total"}
        b = mt["benson|Month Total|2026-11-01"]
        self.assertEqual((b["Net_Sales"], b["Transactions"], b["Days_In_Period"]), (1515, 21, 11))
        self.assertAlmostEqual(b["Avg_Basket"], 72.14)
        self.assertEqual((b["Net_Sales_1_Month_Ago"], b["Net_Sales_2_Months_Ago"], b["Net_Sales_3_Months_Ago"]),
                         (9300, 6000, 3100))                       # Oct, Sep, Aug in full
        self.assertEqual((b["Orders_1_Month_Ago"], b["Prev_3M_Avg_Basket"]), (62, 150.0))   # Oct AOV
        self.assertEqual(b["Prev_3M_Transactions"], 4500)          # projected: 150/day x 30
        self.assertEqual(b["Projected_Orders"], 60)
        self.assertEqual(b["Net_Sales_vs_3M_Pct"], 0)             # "New Customers 1 Month Ago" (none in data)
        cmp_ = {r["Dimension"]: r for r in z.rows.values()
                if r.get("Stat_Type") == "Month Compare" and r["Store"] == "Earth's Healing - Benson"
                and r["Period_Start"] == "2026-11-01"}
        self.assertEqual(sorted(cmp_), ["2026-08 Aug", "2026-09 Sep", "2026-10 Oct", "2026-11 Nov"])
        self.assertEqual((cmp_["2026-10 Oct"]["Net_Sales"], cmp_["2026-10 Oct"]["Transactions"],
                          cmp_["2026-10 Oct"]["Avg_Basket"]), (9300, 62, 150.0))
        self.assertEqual(cmp_["2026-11 Nov"]["Net_Sales"], 1515)
        for k in ("Transactions", "Net_Sales_1_Month_Ago", "Orders_1_Month_Ago", "Prev_3M_Transactions"):
            self.assertIsInstance(b[k], int, k)  # Zoho integer fields reject 20.0
        oct_ = mt["benson|Month Total|2026-10-01"]
        self.assertEqual(oct_["Prev_3M_Transactions"], oct_["Net_Sales"])  # finished month: projection = actual
        a = mt["all|Month Total|2026-11-01"]
        self.assertEqual((a["Store"], a["Net_Sales"], a["Transactions"]), ("All Stores", 2015, 31))
        self.assertIsNone(mt["river|Month Total|2026-11-01"].get("Net_Sales_1_Month_Ago"))  # no River history


class PartialHistoryTests(unittest.TestCase):
    def test_partial_first_month_is_not_a_baseline(self):
        z, rep = FakeZoho(), Report()
        rs = retail.RetailSync(z, rep)

        def day(d, net, tx):
            k = f"b|{d}"
            z.rows[k] = {"id": k, "Stat_Key": k, "Store": "S", "Stat_Type": "Daily Total", "Period_Start": d,
                         "Net_Sales": net, "Transactions": tx}
        for dd in (28, 29, 30):           # history starts 28 June
            day(f"2026-06-{dd}", 100, 1)
        for dd in range(1, 32):
            day(f"2026-07-{dd:02d}", 100, 1)
        for dd in range(1, 11):           # August so far: 1-10 (today is the 11th, no sales yet)
            day(f"2026-08-{dd:02d}", 120, 1)
        rs.month_totals({"S": "s"}, dt.date(2026, 6, 1), dt.date(2026, 8, 11))
        mt = {r["Stat_Key"]: r for r in z.rows.values() if r.get("Stat_Type") == "Month Total"}
        self.assertIsNone(mt["s|Month Total|2026-07-01"].get("Net_Sales_1_Month_Ago"))   # June too short
        # a stale value from an older run is cleared
        z.rows[[k for k, r in z.rows.items() if r.get("Stat_Key") == "s|Month Total|2026-07-01"][0]]["Net_Sales_vs_3M_Pct"] = 946
        rs.month_totals({"S": "s"}, dt.date(2026, 6, 1), dt.date(2026, 8, 11))
        mt = {r["Stat_Key"]: r for r in z.rows.values() if r.get("Stat_Type") == "Month Total"}
        self.assertIsNone(mt["s|Month Total|2026-07-01"]["Net_Sales_vs_3M_Pct"])
        aug = mt["s|Month Total|2026-08-01"]
        self.assertEqual((aug["Days_In_Period"], aug["Net_Sales_1_Month_Ago"], aug["Prev_3M_Transactions"]),
                         (10, 3100, 3720))   # July in full; 120/day x 31

    def test_closed_day_counts_as_passed(self):
        z, rep = FakeZoho(), Report()
        rs = retail.RetailSync(z, rep)
        for dd in range(1, 11):
            if dd == 5:
                continue                  # store closed on the 5th
            k = f"s|{dd}"
            z.rows[k] = {"id": k, "Stat_Key": k, "Store": "S", "Stat_Type": "Daily Total",
                         "Period_Start": f"2026-11-{dd:02d}", "Net_Sales": 100, "Transactions": 1}
        rs.month_totals({"S": "s"}, dt.date(2026, 11, 1), dt.date(2026, 11, 11))
        nov = [r for r in z.rows.values() if r.get("Stat_Key") == "s|Month Total|2026-11-01"][0]
        self.assertEqual(nov["Prev_3M_Transactions"], 2700)   # 900 over 10 days passed -> 90/day x 30


class MonthBoundaryTests(unittest.TestCase):
    def test_late_evening_sale_stays_in_its_local_month(self):
        # 05:00 UTC on 1 Oct = 22:00 on 30 Sep in Arizona
        sales = [tx(10, "2026-09-15T18:00:00Z", [item(1, 1, 100.0)]),
                 tx(11, "2026-10-01T05:00:00Z", [item(1, 1, 50.0)]),
                 tx(12, "2026-10-10T18:00:00Z", [item(2, 1, 20.0)])]
        z, rep = FakeZoho(), Report()
        rs = retail.RetailSync(z, rep)
        rs.sync_store("benson", "B", FakeDutchie(sales), dt.datetime(2026, 9, 1, 7, tzinfo=dt.timezone.utc),
                      dt.datetime(2026, 11, 1, 7, tzinfo=dt.timezone.utc), seen=set())
        by_key = {r["Stat_Key"]: r for r in z.rows.values()}
        self.assertEqual(by_key["benson|Category|2026-09-01|Flower"]["Net_Sales"], 150.0)
        self.assertEqual(by_key["benson|Daily Total|2026-09-15"]["Net_Sales"], 100.0)
        self.assertEqual(by_key["benson|Daily Total|2026-09-30"]["Net_Sales"], 50.0)
        self.assertEqual(by_key["benson|Category|2026-10-01|Edibles"]["Net_Sales"], 20.0)
        self.assertNotIn("benson|Category|2026-10-01|Flower", by_key)


class StoreGroupTests(unittest.TestCase):
    def test_brand_and_arizona_totals(self):
        z, rep = FakeZoho(), Report()
        rs = retail.RetailSync(z, rep)
        stores = {"Earth's Healing - Benson": ("benson", 100), "Earth's Healing - River": ("river", 50),
                  "D2 - Downtown": ("downtown", 30), "D2 - Eastside": ("eastside", 20)}
        for store, (_, net) in stores.items():
            for dd in range(1, 6):
                k = f"{store}|{dd}"
                z.rows[k] = {"id": k, "Stat_Key": k, "Store": store, "Stat_Type": "Daily Total",
                             "Period_Start": f"2026-11-{dd:02d}", "Net_Sales": net, "Transactions": 1}
        # a stray combined daily row must not be counted twice
        z.rows["x"] = {"id": "x", "Stat_Key": "x", "Store": "All Stores", "Stat_Type": "Daily Total",
                       "Period_Start": "2026-11-01", "Net_Sales": 9999, "Transactions": 9}
        rs.month_totals({s: c for s, (c, _) in stores.items()}, dt.date(2026, 11, 1), dt.date(2026, 11, 5))
        mt = {r["Stat_Key"]: r for r in z.rows.values() if r.get("Stat_Type") == "Month Total"}
        self.assertEqual((mt["all|Month Total|2026-11-01"]["Store"], mt["all|Month Total|2026-11-01"]["Net_Sales"]),
                         ("All Stores", 750))
        self.assertEqual((mt["d2all|Month Total|2026-11-01"]["Store"], mt["d2all|Month Total|2026-11-01"]["Net_Sales"]),
                         ("D2 - All Stores", 250))
        az = mt["az|Month Total|2026-11-01"]
        self.assertEqual((az["Store"], az["Net_Sales"], az["Transactions"], az["Avg_Basket"]),
                         ("Arizona - All Stores", 1000, 20, 50.0))

    def test_earths_healing_only_has_no_arizona_row(self):
        z, rep = FakeZoho(), Report()
        rs = retail.RetailSync(z, rep)
        for store in ("Earth's Healing - Benson", "Earth's Healing - River"):
            z.rows[store] = {"id": store, "Stat_Key": store, "Store": store, "Stat_Type": "Daily Total",
                             "Period_Start": "2026-11-01", "Net_Sales": 10, "Transactions": 1}
        rs.month_totals({"Earth's Healing - Benson": "benson", "Earth's Healing - River": "river"},
                        dt.date(2026, 11, 1), dt.date(2026, 11, 1))
        stores = {r["Store"] for r in z.rows.values() if r.get("Stat_Type") == "Month Total"}
        self.assertEqual(stores, {"Earth's Healing - Benson", "Earth's Healing - River", "All Stores"})


if __name__ == "__main__":
    unittest.main()


class NeverSoldTests(unittest.TestCase):
    def test_never_sold_counts_from_history_start(self):
        inv = [{"productId": 1, "quantityAvailable": 5, "unitCost": 2},
               {"productId": 2, "quantityAvailable": 3, "unitCost": 2}]
        today = dt.date(2026, 11, 27)
        rows = retail.inventory_rows(inv, [], today, {1: dt.date(2026, 11, 20)}, dt.date(2026, 6, 26))
        days = {r[3]["Dutchie_Product_ID"]: r[4]["Days_Since_Last_Sale"] for r in rows.values()}
        self.assertEqual(days, {"1": 7, "2": 154})


class LeftoverDailyRowTests(unittest.TestCase):
    def test_leftover_daily_detail_rows_are_deleted(self):
        sales = [tx(10, "2026-09-15T18:00:00Z", [item(1, 1, 100.0)])]
        z, rep = FakeZoho(), Report()
        z.rows["daily"] = {"id": "daily", "Stat_Key": "benson|Category|2026-09-15|Flower", "Store": "B",
                           "Stat_Type": "Category", "Period_Start": "2026-09-15", "Net_Sales": 100}
        retail.RetailSync(z, rep).sync_store("benson", "B", FakeDutchie(sales),
                                             dt.datetime(2026, 9, 1, 7, tzinfo=dt.timezone.utc),
                                             dt.datetime(2026, 10, 1, 7, tzinfo=dt.timezone.utc), seen=set())
        self.assertNotIn("daily", z.rows)
        self.assertIn("benson|Category|2026-09-01|Flower", {r["Stat_Key"] for r in z.rows.values()})
