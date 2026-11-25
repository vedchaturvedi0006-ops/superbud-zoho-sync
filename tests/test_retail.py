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
        inv = "Stat_Type = 'Inventory'" in where
        m = re.search(r"between '([\d-]+)' and '([\d-]+)'", where)
        out = []
        for r in self.rows.values():
            if (r["Stat_Type"] == "Inventory") != inv:
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
        self.assertEqual((inv["Qty_On_Hand"], inv["Inventory_Value"], inv["Days_Since_Last_Sale"] is not None),
                         (12.0, 120.0, True))
        calls = z.calls
        new, upd = rs.sync_store("benson", "Earth's Healing - Benson", FakeDutchie(SALES), start, end)
        self.assertEqual((new, upd), (0, 0))
        self.assertEqual(z.calls, calls)


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


if __name__ == "__main__":
    unittest.main()
