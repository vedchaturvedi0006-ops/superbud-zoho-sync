import datetime as dt
import os
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sbsync import inventory  # noqa: E402
from sbsync.sync import Report  # noqa: E402

TZ = ZoneInfo("America/Phoenix")
TODAY = dt.date(2026, 11, 25)


def sale(tid, day, pid, qty, price=10.0, **kw):
    d = dict(transactionId=tid, transactionDate=f"{day}T19:00:00Z", isVoid=False, isReturn=False,
             items=[{"productId": pid, "quantity": qty, "totalPrice": price * qty, "totalDiscount": 0}])
    d.update(kw)
    return d


def days_ago(n):
    return (TODAY - dt.timedelta(days=n)).isoformat()


PRODUCTS = [
    {"productId": 1, "productName": "Blue Dream 3.5g", "masterCategory": "Flower", "category": "Eighths",
     "brandName": "House", "vendorName": "Grower A", "sku": "BD-35", "strain": "Blue Dream", "strainType": "Hybrid",
     "thcContent": 24.5, "thcContentUnit": "%", "unitWeight": 3.5, "unitWeightUnit": "g", "price": 30, "isActive": True},
    {"productId": 2, "productName": "Mango Gummies", "masterCategory": "Edibles", "sku": "MG-10", "price": 20},
    {"productId": 3, "productName": "Old Pen", "masterCategory": "Vape Cartridges", "sku": "OP-1", "price": 40},
]
INV = [{"productId": 1, "quantityAvailable": 10, "allocatedQuantity": 1, "unitCost": 12},
       {"productId": 2, "quantityAvailable": 500, "unitCost": 5},
       {"productId": 3, "quantityAvailable": 4, "unitCost": 15}]
# Blue Dream: 2 a day for the last 56 days -> 2/day, 10 on hand -> runs out in 5 days
SALES = [sale(100 + i, days_ago(i), 1, 2) for i in range(1, 57)]
# Gummies: 1 a day -> 500 days of stock -> overstock
SALES += [sale(300 + i, days_ago(i), 2, 1) for i in range(1, 57)]
SALES += [sale(999, days_ago(3), 1, 1, isVoid=True)]


class ForecastTests(unittest.TestCase):
    def test_store_rows_forecast_and_details(self):
        rows = inventory.store_rows(PRODUCTS, INV, SALES, TZ, TODAY)
        bd = rows[1][0]
        self.assertEqual((bd["SKU"], bd["Brand"], bd["Vendor"], bd["Strain_Type"], bd["THC"], bd["Size"]),
                         ("BD-35", "House", "Grower A", "Hybrid", "24.5 %", "3.5 g"))
        self.assertEqual(bd["Avg_Daily_Units"], 2.0)
        self.assertEqual(bd["Forecast_Units_3_Weeks"], 42)
        self.assertEqual(bd["Days_Of_Stock"], 5)
        self.assertEqual(bd["Stock_Status"], "Runs Out Within 3 Weeks")
        self.assertEqual(bd["Reorder_Units"], 2 * 28 - 10)          # 3 weeks + 1 week buffer
        self.assertEqual(bd["Projected_Stockout_Date"], "2026-11-30")
        self.assertEqual((bd["Stock_Value"], bd["Retail_Value"]), (120.0, 300.0))
        self.assertEqual(rows[2][0]["Stock_Status"], "Overstock (120+ Days)")
        self.assertEqual(rows[3][0]["Stock_Status"], "No Sales in 90 Days")
        self.assertEqual(rows[3][0]["Reorder_Units"], 0)

    def test_combined_by_sku(self):
        a = inventory.store_rows(PRODUCTS, INV, SALES, TZ, TODAY)
        b = inventory.store_rows(PRODUCTS, INV, SALES, TZ, TODAY)
        c = inventory.combine([a, b], TODAY)
        bd = c["bd-35"][0]
        self.assertEqual((bd["Qty_On_Hand"], bd["Avg_Daily_Units"], bd["Forecast_Units_3_Weeks"]), (20, 4.0, 84))
        self.assertEqual(bd["Stock_Value"], 240.0)


class FakeZoho:
    def __init__(self):
        self.rows, self.calls = {}, 0

    def coql_all(self, cols, module, where=""):
        store = where.split("'", 1)[1].rsplit("'", 1)[0].replace("''", "'")
        return [dict(r) for r in self.rows.values() if r["Store"] == store]

    def create(self, module, recs):
        self.calls += 1
        for r in recs:
            rid = str(len(self.rows) + 1)
            self.rows[rid] = dict(r, id=rid)
        return [{"status": "success"} for _ in recs]

    def update(self, module, recs):
        self.calls += 1
        for r in recs:
            self.rows[r["id"]].update(r)
        return [{"status": "success"} for _ in recs]


class WriteTests(unittest.TestCase):
    def test_second_write_changes_nothing_and_missing_goes_out_of_stock(self):
        z = FakeZoho()
        s = inventory.InventorySync(z, Report())
        rows = inventory.store_rows(PRODUCTS, INV, SALES, TZ, TODAY)
        s.write("benson", "Earth's Healing - Benson", rows)
        self.assertEqual(len(z.rows), 3)
        calls = z.calls
        s.write("benson", "Earth's Healing - Benson", rows)
        self.assertEqual(z.calls, calls)
        rows.pop(3)
        s.write("benson", "Earth's Healing - Benson", rows)
        old = next(r for r in z.rows.values() if r["Inventory_Key"] == "benson|3")
        self.assertEqual((old["Qty_On_Hand"], old["Stock_Status"]), (0, "Out of Stock"))


if __name__ == "__main__":
    unittest.main()
