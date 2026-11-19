"""Offline tests: fake Superbud + fake Zoho, no network.
Run: python -m unittest discover tests"""
import copy
import itertools
import os
import unittest

os.environ.setdefault("SUPERBUD_API_KEY", "test")
os.environ.setdefault("SUPERBUD_DISTRIBUTOR_ID", "dist-1")

from sbsync import mapping  # noqa: E402
from sbsync.sync import Sync  # noqa: E402

_ids = itertools.count(1000)


class FakeZoho:
    """Keeps records in memory and behaves like the parts of Zoho the sync uses."""

    def __init__(self, live=True):
        self.live = live
        self.writes = []
        self.db = {m: {} for m in ("Accounts", "Contacts", "Products", "Deals", "Sales_Orders", "Notes")}

    def seed(self, module, record):
        rid = str(next(_ids))
        self.db[module][rid] = dict(record, id=rid)
        return rid

    def coql_all(self, select, module, where="id is not null"):
        rows = [copy.deepcopy(r) for r in self.db[module].values()]
        if "Superbud_Order_ID is not null" in where:
            rows = [r for r in rows if r.get("Superbud_Order_ID")]
        return rows

    def get_record(self, module, rid):
        return copy.deepcopy(self.db[module][rid])

    def users(self):
        return [{"id": "u1", "email": "rep@example.com"}]

    def create(self, module, records):
        out = []
        for r in records:
            self.writes.append(("create", module, r))
            rid = str(next(_ids))
            rec = copy.deepcopy(r)
            if module == "Sales_Orders":
                for i, item in enumerate(rec["Ordered_Items"]):
                    item["id"] = f"row{rid}-{i}"
            self.db[module][rid] = dict(rec, id=rid)
            out.append({"status": "success", "details": {"id": rid}})
        return out

    def update(self, module, records):
        out = []
        for r in records:
            self.writes.append(("update", module, r))
            rec = self.db[module][r["id"]]
            for k, v in r.items():
                if k == "Ordered_Items":
                    rows = {row["id"]: row for row in rec.get("Ordered_Items", [])}
                    for item in v:
                        if "_delete" in item:
                            rows.pop(item["id"], None)
                        elif item.get("id"):
                            rows[item["id"]].update(item)
                        else:
                            nid = f"new{next(_ids)}"
                            rows[nid] = dict(item, id=nid)
                    rec["Ordered_Items"] = list(rows.values())
                else:
                    rec[k] = v
            out.append({"status": "success", "details": {"id": r["id"]}})
        return out


def location(lid, name, cust_id, cust_name):
    return {"id": lid, "name": name, "customer": {"id": cust_id, "name": cust_name},
            "address": {"line1": "1 Main St", "line2": "Suite 2", "city": "Main Street", "state": "MI",
                        "zip": "49417", "country": "US"},
            "businessAddress": None, "assignedSalesRep": {"id": "r1", "email": "rep@example.com"}}


def product(pid, name, collection, options):
    return {"id": pid, "name": name, "description": None, "brand": {"name": "Test Brand"},
            "collections": {"edges": [{"node": {"id": "c", "name": collection}}]}, "options": options}


def option(oid, price, qty, pack=50):
    return {"id": oid, "name": f"1g - Case of {pack}", "sku": None, "basePriceDollars": price,
            "displayQuantityAvailable": qty, "packSize": pack, "packType": "CASE"}


class FakeSuperbud:
    def __init__(self):
        self.locations = [
            location("L1", "Main Street", "C1", "Maple Leaf Co"),
            location("L2", "Second Street", "C1", "Maple Leaf Co"),
            location("L3", "Green Corner", "C2", "Green Corner"),
        ]
        self.licenses = {"AU-R-000001": "L1", "AU-R-000002": "L3"}
        self.products_list = [
            product("P1", "Mojito Rusty Nail Infused Pre-Rolls", "Rusty Nail Infused Pre-Rolls",
                    [option("O1", 75.0, 5)]),
            product("P2", "Animal Tsunami Bulk Flower", "Bulk Flower", [option("O2", 700.0, 12, pack=1)]),
        ]
        self.orders = [{
            "id": "ORD1", "orderNumber": 7, "status": "REQUIRES_SUPPLIER_REVIEW", "approvalState": "NEEDS_SUPPLIER_REVIEW",
            "version": 1, "createdAt": "2026-11-18T10:00:00Z", "approvedAt": None, "dueDate": None,
            "subtotal": 150, "total": 150, "shippingFee": 0, "localTax": 0, "stateTax": 0, "credits": 0,
            "manualAdjustments": 0, "customer": {"id": "C1", "name": "Maple Leaf Co"},
            "customerLocation": {"id": "L1", "name": "Main Street"},
            "buyerRep": {"id": "B1", "firstName": "Test", "lastName": "Buyer", "email": "buyer@example.com",
                         "phoneNumber": "231"},
            "salesRep": {"id": "r1", "email": "rep@example.com"}, "shippingAddress": None,
            "lineItems": [{"id": "LI1", "quantity": 2, "basePriceDollars": 75.0, "priceDollars": 75.0,
                           "discountDollars": 0, "totalDollars": 150, "option": {"name": "1g", "currentModel": {"id": "O1"}}}],
        }]

    def distributor_id(self):
        return "dist-1"

    def customer_locations(self):
        return copy.deepcopy(self.locations)

    def locations_by_search(self, text):
        lid = self.licenses.get(text)
        return [{"id": lid, "name": ""}] if lid else []

    def products(self):
        return copy.deepcopy(self.products_list)

    def orders_since(self, iso):
        return copy.deepcopy(self.orders)

    def orders_by_ids(self, ids):
        return [copy.deepcopy(o) for o in self.orders if o["id"] in ids]


def setup():
    z, sb = FakeZoho(), FakeSuperbud()
    z.seed("Accounts", {"Account_Name": "Maple Leaf Co - AU-R-000001", "License_number": "AU-R-000001"})
    z.seed("Accounts", {"Account_Name": "Green Corner", "License_number": "AU-R-000002"})
    z.seed("Contacts", {"First_Name": "Test", "Last_Name": "Buyer", "Email": "buyer@example.com"})
    z.seed("Products", {"Product_Name": "Rusty Nail - Mojito", "Product_Category": "Pre Rolls",
                        "Sub_Category": "Infused Rusty Nail", "Flavour": "Mojito", "Unit_Price": 1.5, "Qty_in_Stock": 0})
    return z, sb


def run(z, sb):
    return Sync(z, sb, live=True).run()


class SyncTests(unittest.TestCase):
    def test_first_run_links_and_creates(self):
        z, sb = setup()
        report = run(z, sb)
        accs = {a["Account_Name"]: a for a in z.db["Accounts"].values()}
        # matched by license, name kept
        self.assertEqual(accs["Maple Leaf Co - AU-R-000001"]["Superbud_Customer_Location_ID"], "L1")
        self.assertEqual(accs["Green Corner"]["Superbud_Customer_Location_ID"], "L3")
        # Second Street has no license match and no orders -> not created yet (on_activity)
        self.assertNotIn("Maple Leaf Co - Second Street", accs)
        self.assertEqual(len(z.db["Accounts"]), 2)
        # product matched by sub-category + flavour, price per unit
        mojito = next(p for p in z.db["Products"].values() if p["Product_Name"] == "Rusty Nail - Mojito")
        self.assertEqual(mojito["Superbud_Product_Option_ID"], "O1")
        self.assertEqual(mojito["Unit_Price"], 1.5)
        self.assertEqual(mojito["Qty_in_Stock"], 5)
        # new product created
        self.assertTrue(any(p["Product_Name"] == "Bulk Flower - Animal Tsunami" for p in z.db["Products"].values()))
        # contact matched by email, not duplicated
        self.assertEqual(len(z.db["Contacts"]), 1)
        self.assertEqual(next(iter(z.db["Contacts"].values()))["Superbud_Buyer_User_ID"], "B1")
        # order in review -> open deal, no sales order yet
        deal = next(iter(z.db["Deals"].values()))
        self.assertEqual(deal["Stage"], "open")
        self.assertEqual(deal["Deal_Name"], "SB-7 - Main Street")
        self.assertEqual(len(z.db["Sales_Orders"]), 0)
        self.assertFalse(any(k.endswith("error") for k in report.counts), report.render(True))

    def test_second_run_writes_nothing(self):
        z, sb = setup()
        run(z, sb)
        z.writes.clear()
        run(z, sb)
        self.assertEqual(z.writes, [], z.writes)

    def test_confirmed_order_wins_deal_and_creates_sales_order(self):
        z, sb = setup()
        run(z, sb)
        sb.orders[0].update(status="CONFIRMED", approvalState="APPROVED", version=2)
        run(z, sb)
        deal = next(iter(z.db["Deals"].values()))
        self.assertEqual(deal["Stage"], "Closed Won")
        so = next(iter(z.db["Sales_Orders"].values()))
        self.assertEqual(so["Status"], "Approved")
        self.assertEqual(so["Superbud_Order_Version"], 2)
        self.assertEqual(len(so["Ordered_Items"]), 1)
        # line change: quantity up, new line added -> same row kept, one row added
        sb.orders[0]["lineItems"][0]["quantity"] = 4
        sb.orders[0]["lineItems"].append({"id": "LI2", "quantity": 1, "basePriceDollars": 700.0, "priceDollars": 700.0,
                                          "discountDollars": 0, "totalDollars": 700,
                                          "option": {"name": "1 lb", "currentModel": {"id": "O2"}}})
        sb.orders[0]["version"] = 3
        old_row = so["Ordered_Items"][0]["id"]
        run(z, sb)
        so = next(iter(z.db["Sales_Orders"].values()))
        self.assertEqual(len(so["Ordered_Items"]), 2)
        kept = next(r for r in so["Ordered_Items"] if r["id"] == old_row)
        self.assertEqual(kept["Quantity"], 4)
        # line removed in Superbud -> removed in Zoho
        sb.orders[0]["lineItems"].pop()
        sb.orders[0]["version"] = 4
        run(z, sb)
        so = next(iter(z.db["Sales_Orders"].values()))
        self.assertEqual(len(so["Ordered_Items"]), 1)
        # cancelled -> Closed Lost + Cancelled
        sb.orders[0].update(status="CANCELLED", version=5)
        run(z, sb)
        self.assertEqual(next(iter(z.db["Deals"].values()))["Stage"], "Closed Lost")
        self.assertEqual(next(iter(z.db["Sales_Orders"].values()))["Status"], "Cancelled")

    def test_stock_and_price_changes_update_product(self):
        z, sb = setup()
        run(z, sb)
        sb.products_list[0]["options"][0].update(displayQuantityAvailable=2, basePriceDollars=100.0)
        z.writes.clear()
        run(z, sb)
        mojito = next(p for p in z.db["Products"].values() if p.get("Superbud_Product_Option_ID") == "O1")
        self.assertEqual(mojito["Qty_in_Stock"], 2)
        self.assertEqual(mojito["Unit_Price"], 2.0)
        self.assertEqual([w[1] for w in z.writes], ["Products"])

    def test_removed_option_marks_product_inactive(self):
        z, sb = setup()
        run(z, sb)
        sb.products_list.pop()  # Animal Tsunami removed from Superbud
        run(z, sb)
        tsunami = next(p for p in z.db["Products"].values() if p["Product_Name"] == "Bulk Flower - Animal Tsunami")
        self.assertIs(tsunami["Product_Active"], False)
        self.assertEqual(len(z.db["Products"]), 2)

    def test_regular_runs_skip_license_matching_until_full_pass(self):
        z, sb = setup()
        run(z, sb)  # first run is always a full pass
        sb.locations.append(location("L9", "New Store", "C9", "Brand New Co"))
        calls = []
        sb.locations_by_search = lambda text: calls.append(text) or []
        s = Sync(z, sb, live=True)
        s.now = s.now.replace(hour=(int(os.environ.get("FULL_PASS_HOUR_UTC", "21")) + 1) % 24)
        s.run()
        self.assertEqual(calls, [])  # no license searches on a regular run
        self.assertFalse(any(a["Account_Name"].startswith("Brand New") for a in z.db["Accounts"].values()))

    def test_store_with_new_order_is_created_with_contact_and_deal(self):
        z, sb = setup()
        run(z, sb)
        sb.locations.append(location("L9", "New Store", "C9", "Brand New Co"))
        new_order = copy.deepcopy(sb.orders[0])
        new_order.update(id="ORD9", orderNumber=9, customerLocation={"id": "L9", "name": "New Store"},
                         customer={"id": "C9", "name": "Brand New Co"},
                         buyerRep={"id": "B9", "firstName": "New", "lastName": "Person", "email": "new@example.com"})
        sb.orders.append(new_order)
        s = Sync(z, sb, live=True)
        s.now = s.now.replace(hour=(int(os.environ.get("FULL_PASS_HOUR_UTC", "21")) + 1) % 24)  # regular run
        s.run()
        acc = next(a for a in z.db["Accounts"].values() if a["Account_Name"] == "Brand New Co")
        self.assertEqual(acc["Superbud_Sync_Status"], "Created by Sync")
        contact = next(c for c in z.db["Contacts"].values() if c.get("Email") == "new@example.com")
        self.assertEqual(contact["Account_Name"]["id"], acc["id"])
        deal = next(d for d in z.db["Deals"].values() if d["Superbud_Order_ID"] == "ORD9")
        self.assertEqual(deal["Account_Name"]["id"], acc["id"])

    def test_create_all_mode_creates_every_missing_store(self):
        os.environ["CREATE_NEW_DISPENSARIES"] = "true"
        try:
            z, sb = setup()
            run(z, sb)
            self.assertIn("Maple Leaf Co - Second Street", {a["Account_Name"] for a in z.db["Accounts"].values()})
        finally:
            del os.environ["CREATE_NEW_DISPENSARIES"]

    def test_quiet_report_hides_names(self):
        z, sb = setup()
        text = run(z, sb).render(True, details=False)
        self.assertNotIn("Maple Leaf", text)
        self.assertNotIn("Test Buyer", text)
        self.assertIn("Dispensary: updated", text)

    def test_cancelled_order_never_in_zoho_is_ignored(self):
        z, sb = setup()
        sb.orders[0]["status"] = "CANCELLED"
        run(z, sb)
        self.assertEqual(len(z.db["Deals"]), 0)

    def test_duplicate_superbud_listing_gets_unique_name(self):
        z, sb = setup()
        sb.products_list.append(product("P3", "Mojito Rusty Nail Infused Pre-Rolls", "Rusty Nail Infused Pre-Rolls",
                                        [option("O3", 75.0, 0)]))
        run(z, sb)
        names = [p["Product_Name"] for p in z.db["Products"].values()]
        self.assertEqual(len(names), len(set(n.lower() for n in names)), names)

    def test_common_fields_filled_everywhere(self):
        z, sb = setup()
        o = sb.orders[0]
        o.update(status="CONFIRMED", approvalState="APPROVED", credits=10.0, shippingFee=5.0, manualAdjustments=0,
                 source="INTERNAL_REP", balanceDueDollars=145.0,
                 paymentTerms={"paymentTerm": "NET_30", "billOn": "DELIVERY", "delinquentAfterDays": 45,
                               "preferredPaymentMethod": "ACH"},
                 placedByUser={"id": "staff1", "firstName": "Office", "lastName": "Staff", "email": "ops@example.com"})
        sb.products_list[0]["options"][0]["displayQuantityReserved"] = 3
        run(z, sb)
        acc = next(a for a in z.db["Accounts"].values() if a.get("Superbud_Customer_Location_ID") == "L1")
        self.assertEqual((acc["Shipping_State"], acc["Shipping_Country"]), ("Michigan", "United States"))
        self.assertEqual(acc["Shipping_Flat_House_No_Building_Apartment_Name"], "Suite 2")
        self.assertEqual(acc["Shipping_Street"], "1 Main St")
        deal = next(iter(z.db["Deals"].values()))
        self.assertEqual(deal["Type"], "New Business")
        so = next(iter(z.db["Sales_Orders"].values()))
        self.assertEqual(so["Purchase_Order"], "SB-7")
        self.assertEqual(so["Customer_No"], "AU-R-000001")
        self.assertEqual(so["Discount"], 10.0)
        self.assertEqual(so["Adjustment"], 5.0)
        self.assertEqual(so["Billing_State"], "Michigan")
        self.assertIn("Net 30", so["Terms_and_Conditions"])
        self.assertIn("sales rep", so["Description"])
        self.assertEqual(so["Owner"], {"id": "u1"})
        self.assertEqual(so["Ordered_Items"][0]["Description"], "1g")
        # rep-placed order: the submitting staff member is not a customer contact
        self.assertFalse(any(c.get("Email") == "ops@example.com" for c in z.db["Contacts"].values()))
        buyer = next(c for c in z.db["Contacts"].values() if c.get("Superbud_Buyer_User_ID") == "B1")
        self.assertEqual(buyer["Mailing_State"], "Michigan")
        mojito = next(p for p in z.db["Products"].values() if p.get("Superbud_Product_Option_ID") == "O1")
        self.assertEqual(mojito["Qty_in_Demand"], 3)
        # nothing changes on a second run
        z.writes.clear()
        run(z, sb)
        self.assertEqual([w for w in z.writes if w[1] != "Notes"], [], z.writes)

    def test_draft_order_becomes_open_deal_without_sales_order(self):
        z, sb = setup()
        sb.orders[0]["status"] = "DRAFT"
        run(z, sb)
        self.assertEqual(next(iter(z.db["Deals"].values()))["Stage"], "open")
        self.assertEqual(len(z.db["Sales_Orders"]), 0)

    def test_history_and_payments_become_notes_once(self):
        z, sb = setup()
        o = sb.orders[0]
        o["history"] = [{"id": "h1", "occurredAt": "2026-11-18T10:00:00Z", "eventJson": '{"type": "ORDER_SUBMITTED"}'},
                        {"id": "h2", "occurredAt": "2026-11-18T11:00:00Z", "eventJson": '{"type": "LINE_EDITED", "qty": 4}'}]
        o["payments"] = [{"id": "p1", "amountDollars": 100.0, "method": "ACH", "paymentDate": "2026-11-19T00:00:00Z"}]
        run(z, sb)
        titles = sorted(n["Note_Title"] for n in z.db["Notes"].values())
        self.assertEqual(len(titles), 3, titles)
        self.assertTrue(any("Order Submitted" in t for t in titles))
        self.assertTrue(any("Payment: $100.00" in t for t in titles))
        run(z, sb)  # no new activity -> no new notes
        self.assertEqual(len(z.db["Notes"]), 3)
        o["history"].append({"id": "h3", "occurredAt": "2026-11-19T09:00:00Z", "eventJson": '{"type": "ORDER_SHIPPED"}'})
        run(z, sb)
        self.assertEqual(len(z.db["Notes"]), 4)

    def test_order_dates_and_balance_on_deal_without_repeat_updates(self):
        z, sb = setup()
        sb.orders[0].update(status="SHIPPED", shippedAt="2026-11-19T08:30:00.123Z", balanceDueDollars=150.0,
                            isPastDue=False, daysPastDue=0)
        run(z, sb)
        deal = next(iter(z.db["Deals"].values()))
        self.assertEqual(deal["Superbud_Shipped_At"], "2026-11-19T08:30:00+00:00")
        self.assertEqual(deal["Superbud_Balance_Due"], 150.0)
        # Zoho hands datetimes back in local time; that must not count as a change
        deal["Superbud_Shipped_At"] = "2026-11-19T14:00:00+05:30"
        z.writes.clear()
        run(z, sb)
        self.assertFalse([w for w in z.writes if w[1] == "Deals"], z.writes)

    def test_contact_details_follow_superbud_and_placed_by_added(self):
        z, sb = setup()
        run(z, sb)
        o = sb.orders[0]
        o["buyerRep"]["phoneNumber"] = "999"
        o["placedByUser"] = {"id": "B7", "firstName": "Other", "lastName": "Buyer", "email": "other@example.com"}
        run(z, sb)
        buyer = next(c for c in z.db["Contacts"].values() if c.get("Superbud_Buyer_User_ID") == "B1")
        self.assertEqual(buyer["Phone"], "999")
        self.assertTrue(any(c.get("Email") == "other@example.com" for c in z.db["Contacts"].values()))

    def test_sales_rep_never_becomes_contact(self):
        z, sb = setup()
        sb.orders[0]["placedByUser"] = {"id": "r1", "firstName": "Rep", "lastName": "Person", "email": "rep@example.com"}
        run(z, sb)
        self.assertFalse(any(c.get("Email") == "rep@example.com" for c in z.db["Contacts"].values()))

    def test_company_address_change_and_deletion_reach_dispensary(self):
        z, sb = setup()
        run(z, sb)
        sb.locations[0]["address"]["line1"] = "99 New Road"
        sb.locations[0]["customer"]["deletedAt"] = "2026-11-19T00:00:00Z"
        run(z, sb)
        acc = next(a for a in z.db["Accounts"].values() if a.get("Superbud_Customer_Location_ID") == "L1")
        self.assertEqual(acc["Shipping_Street"], "99 New Road")
        self.assertEqual(acc["Superbud_Sync_Status"], "Deleted in Superbud")
        self.assertTrue(acc["Account_Name"].startswith("Maple Leaf Co - AU"))  # name never changed


class MappingTests(unittest.TestCase):
    def test_stages(self):
        self.assertEqual(mapping.deal_stage("DRAFT"), "open")
        self.assertEqual(mapping.deal_stage("REQUIRES_CUSTOMER_REVIEW", "APPROVED"), "Closed Won")
        self.assertEqual(mapping.deal_stage("SHIPPED"), "Closed Won")
        self.assertEqual(mapping.deal_stage("REJECTED"), "Closed Lost")
        self.assertEqual(mapping.sales_order_status("PACKED"), "Approved")
        self.assertEqual(mapping.sales_order_status("DELIVERED"), "Delivered")

    def test_product_classification(self):
        pm = mapping.load_product_map()
        cases = {
            "Reefer Gladness Peach Mango 4-Pack": ("50mg gummies", "Peach Mango"),
            "Reefer Gladness Sour Green Apple Single": ("200mg gummies", "Sour Green Apple"),
            "Gold Rush Flavor Bombs Infused Pre-Rolls": ("Flavor Bombs", "Gold Rush"),
            "HiFi 4G #9 Bulk Flower": ("Bulk Flower", "HiFi 4G #9"),
        }
        for name, (sub, flavour) in cases.items():
            info = mapping.classify_product({"name": name, "collections": {"edges": []}}, pm)
            self.assertEqual((info["Sub_Category"], info["Flavour"]), (sub, flavour), name)


if __name__ == "__main__":
    unittest.main()
