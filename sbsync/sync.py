"""The sync itself. Superbud is the source of truth for locations, products,
stock and orders; Zoho keeps its own names, licenses and sales data.

Duplicate protection: every Zoho record stores its Superbud ID. Each run reads
those IDs from Zoho first, so a record is only created when no match exists."""
import datetime as dt
from collections import Counter, defaultdict

from . import config, mapping
from .http import ApiError
from .zoho import first_id


class Report:
    def __init__(self):
        self.counts = Counter()
        self.lines = []

    def add(self, kind, outcome, text):
        self.counts[f"{kind}: {outcome}"] += 1
        self.lines.append(f"[{kind}] {outcome.upper():<13} {text}")

    def render(self, live, details=True):
        head = ["=" * 78, f"SUPERBUD -> ZOHO SYNC  ({'LIVE' if live else 'DRY RUN - nothing was written'})",
                f"Run at {dt.datetime.now():%Y-%m-%d %H:%M:%S}", "=" * 78]
        summary = [f"  {k:<40} {v}" for k, v in sorted(self.counts.items())]
        if not details:
            return "\n".join(head + ["SUMMARY"] + summary +
                             ["", "(Names and error details are hidden here because these logs are public. "
                                  "Run the sync on your PC to see them.)"])
        return "\n".join(head + ["SUMMARY"] + summary + ["", "DETAILS"] + self.lines)


def _changed(current, wanted):
    """Only the fields whose value actually differs."""
    out = {}
    for key, value in wanted.items():
        have = current.get(key)
        if isinstance(have, dict) and "id" in have:
            have = have["id"]
        if isinstance(value, dict) and "id" in value:
            value_cmp = value["id"]
        else:
            value_cmp = value
        if isinstance(value_cmp, (int, float)) and not isinstance(value_cmp, bool) \
                and isinstance(have, (int, float)) and not isinstance(have, bool):
            if abs(have - value_cmp) < 0.005:
                continue
        elif str(have if have is not None else "") == str(value_cmp if value_cmp is not None else ""):
            continue
        out[key] = value
    return out


class Sync:
    def __init__(self, zoho, superbud, live=False):
        self.z = zoho
        self.sb = superbud
        self.live = live
        self.report = Report()
        self.product_map = mapping.load_product_map()
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    # ================================================================ load ==
    def load_zoho(self):
        z = self.z
        self.accounts = z.coql_all(
            "id, Account_Name, License_number, Superbud_Customer_ID, Superbud_Customer_Location_ID, "
            "Superbud_Location_Name, Superbud_Sync_Status, Billing_Street, Billing_City, Billing_State, "
            "Billing_Code, Billing_Country, Shipping_Street, Shipping_City, Shipping_State, Shipping_Code, "
            "Shipping_Country", "Accounts")
        self.contacts = z.coql_all("id, First_Name, Last_Name, Email, Phone, Superbud_Buyer_User_ID, Account_Name",
                                   "Contacts")
        self.products = z.coql_all("id, Product_Name, Product_Code, Product_Category, Sub_Category, Flavour, "
                                   "Unit_Price, Qty_in_Stock, Brand, Product_Active, Superbud_Product_ID, "
                                   "Superbud_Product_Option_ID", "Products")
        self.deals = z.coql_all("id, Deal_Name, Stage, Amount, Closing_Date, Account_Name, Contact_Name, "
                                "Superbud_Order_ID, Superbud_Order_Number, Superbud_Order_Status, Sync_Source",
                                "Deals", where="Superbud_Order_ID is not null")
        self.sales_orders = z.coql_all("id, Subject, Status, Superbud_Order_ID, Superbud_Order_Version, Deal_Name",
                                       "Sales_Orders", where="Superbud_Order_ID is not null")
        try:
            self.users_by_email = {u["email"].lower(): u["id"] for u in z.users() if u.get("email")}
        except ApiError:
            self.users_by_email = {}

        self.acc_by_loc = {a["Superbud_Customer_Location_ID"]: a for a in self.accounts
                           if a.get("Superbud_Customer_Location_ID")}
        self.contact_by_sb = {c["Superbud_Buyer_User_ID"]: c for c in self.contacts if c.get("Superbud_Buyer_User_ID")}
        self.contact_by_email = {c["Email"].lower(): c for c in self.contacts if c.get("Email")}
        self.prod_by_option = {p["Superbud_Product_Option_ID"]: p for p in self.products
                               if p.get("Superbud_Product_Option_ID")}
        self.deal_by_order = {d["Superbud_Order_ID"]: d for d in self.deals}
        self.so_by_order = {s["Superbud_Order_ID"]: s for s in self.sales_orders}

    def full_pass(self):
        """License matching + new Dispensaries: on the first run, once a day, or when forced."""
        if config.FORCE_FULL_PASS() or not self.acc_by_loc:
            return True
        # whole hour, because GitHub sometimes starts scheduled runs late
        return self.now.hour == config.FULL_PASS_HOUR_UTC()

    def owner(self, user):
        email = ((user or {}).get("email") or "").lower()
        uid = self.users_by_email.get(email)
        return {"id": uid} if uid else None

    # =========================================================== locations ==
    def sync_locations(self):
        locations = self.sb.customer_locations()
        per_customer = Counter((l.get("customer") or {}).get("id") for l in locations)
        sb_ids = {l["id"] for l in locations}
        mode = config.CREATE_NEW_DISPENSARIES()
        unmatched_ids = {l["id"] for l in locations if l["id"] not in self.acc_by_loc}
        # A store with new Superbud activity (an order) that isn't linked yet triggers
        # license matching right away, so its order can be synced on this run.
        active_new = unmatched_ids & self.active_locations
        full = self.full_pass() or bool(active_new)
        waiting = 0

        # 1) First-time matching by license: for Zoho Dispensaries that have a
        #    license but no Superbud ID yet, ask Superbud which location has it.
        #    This takes ~1 request per Dispensary, so it only runs in the daily full pass.
        license_match = {}
        unmatched = [l for l in locations if l["id"] not in self.acc_by_loc]
        if unmatched and not full:
            self.report.add("Dispensary", "skipped",
                            f"{len(unmatched)} unlinked Superbud locations wait for the daily full pass")
            locations = [l for l in locations if l["id"] in self.acc_by_loc]
        if unmatched and full:
            for acc in self.accounts:
                lic = (acc.get("License_number") or "").strip()
                if not lic or acc.get("Superbud_Customer_Location_ID"):
                    continue
                try:
                    hits = [h for h in self.sb.locations_by_search(lic) if h["id"] in sb_ids]
                except ApiError as exc:
                    self.report.add("Dispensary", "error", f"license search {lic}: {exc}")
                    continue
                if len(hits) == 1:
                    license_match.setdefault(hits[0]["id"], []).append(acc)
                elif len(hits) > 1:
                    self.report.add("Dispensary", "needs review",
                                    f"{acc['Account_Name']}: license {lic} matches {len(hits)} Superbud locations")

        creates, updates = [], []
        for loc in locations:
            customer = loc.get("customer") or {}
            wanted = {
                "Superbud_Customer_Location_ID": loc["id"],
                "Superbud_Customer_ID": customer.get("id"),
                "Superbud_Location_Name": loc.get("name"),
            }
            acc = self.acc_by_loc.get(loc["id"])
            if acc is None and loc["id"] in license_match:
                matches = license_match[loc["id"]]
                if len(matches) == 1:
                    acc = matches[0]
                    wanted["Superbud_Sync_Status"] = "Matched"
                else:
                    self.report.add("Dispensary", "needs review",
                                    f"{loc.get('name')}: {len(matches)} Zoho Dispensaries share its license")
                    continue

            if acc is None:
                name = mapping.dispensary_name(loc, per_customer)
                same_name = [a for a in self.accounts if mapping.norm(a.get("Account_Name")) == mapping.norm(name)
                             and not a.get("Superbud_Customer_Location_ID")]
                if len(same_name) == 1:
                    acc = same_name[0]
                    wanted["Superbud_Sync_Status"] = "Needs Review"
                    self.report.add("Dispensary", "needs review",
                                    f"'{name}' linked by name only (no license match) - please confirm")
                elif mode in ("false", "0", "no", "off"):
                    self.report.add("Dispensary", "skipped", f"'{name}' not in Zoho (creation is turned off)")
                    continue
                elif mode == "on_activity" and loc["id"] not in self.active_locations:
                    waiting += 1  # created later, when this store places an order
                    continue
                else:
                    record = dict(wanted, Account_Name=name, Superbud_Sync_Status="Created by Sync")
                    record.update(mapping.zoho_address(loc.get("businessAddress"), "Billing"))
                    record.update(mapping.zoho_address(loc.get("address"), "Shipping"))
                    owner = self.owner(loc.get("assignedSalesRep"))
                    if owner:
                        record["Owner"] = owner
                    creates.append((loc, record))
                    continue

            # Existing Dispensary: store IDs; fill empty address fields only (Zoho names stay as they are)
            for prefix, addr in (("Billing", loc.get("businessAddress")), ("Shipping", loc.get("address"))):
                for key, value in mapping.zoho_address(addr, prefix).items():
                    if not acc.get(key):
                        wanted[key] = value
            diff = _changed(acc, wanted)
            if diff:
                updates.append((acc, loc, dict(diff, id=acc["id"])))
            else:
                self.report.add("Dispensary", "unchanged", acc["Account_Name"])

        if waiting:
            self.report.add("Dispensary", "not needed yet",
                            f"{waiting} Superbud stores not in Zoho - created automatically when they place an order")
        if creates:
            results = self.z.create("Accounts", [r for _, r in creates])
            for (loc, rec), res in zip(creates, results):
                new_id = first_id([res])
                if new_id:
                    rec["id"] = new_id
                    self.acc_by_loc[loc["id"]] = rec
                    self.report.add("Dispensary", "created", rec["Account_Name"])
                else:
                    self.report.add("Dispensary", "error", f"{rec['Account_Name']}: {res}")
        if updates:
            results = self.z.update("Accounts", [r for _, _, r in updates])
            for (acc, loc, rec), res in zip(updates, results):
                if res.get("status") in ("success", "dry_run"):
                    acc.update(rec)
                    self.acc_by_loc[loc["id"]] = acc
                    fields = ", ".join(k for k in rec if k != "id")
                    self.report.add("Dispensary", "updated", f"{acc['Account_Name']} ({fields})")
                else:
                    self.report.add("Dispensary", "error", f"{acc['Account_Name']}: {res}")

        if config.PUSH_LOCATIONS_TO_SUPERBUD():
            self.push_locations(locations)

    def push_locations(self, locations):
        """Zoho -> Superbud: send Zoho's Shipping address to Superbud when it differs."""
        for loc in locations:
            acc = self.acc_by_loc.get(loc["id"])
            if not acc or not acc.get("id") or not acc.get("Shipping_Street"):
                continue
            want = {"line1": acc.get("Shipping_Street"), "city": acc.get("Shipping_City"),
                    "state": acc.get("Shipping_State"), "zip": acc.get("Shipping_Code"),
                    "country": acc.get("Shipping_Country")}
            have = loc.get("address") or {}
            if all(str(want[k] or "") == str(have.get(k) or "") for k in want):
                continue
            if self.live:
                try:
                    self.sb.update_customer_location(loc["id"], address={k: v for k, v in want.items() if v})
                except ApiError as exc:
                    self.report.add("Superbud location", "error", f"{loc.get('name')}: {exc}")
                    continue
            self.report.add("Superbud location", "updated", f"{loc.get('name')}: address from Zoho")

    # ============================================================ products ==
    def sync_products(self):
        sb_products = self.sb.products()
        zoho_by_key = defaultdict(list)
        for p in self.products:
            if not p.get("Superbud_Product_Option_ID"):
                zoho_by_key[mapping.product_key(p.get("Sub_Category"), p.get("Flavour"))].append(p)

        seen_options, creates, updates = set(), [], []
        taken_names = {mapping.norm(p.get("Product_Name")) for p in self.products}
        mode = config.PRODUCT_PRICE_MODE()
        for prod in sb_products:
            info = mapping.classify_product(prod, self.product_map)
            for opt in prod.get("options") or []:
                seen_options.add(opt["id"])
                wanted = {
                    "Superbud_Product_Option_ID": opt["id"],
                    "Superbud_Product_ID": prod["id"],
                    "Unit_Price": mapping.unit_price(opt, mode),
                    "Qty_in_Stock": opt.get("displayQuantityAvailable"),
                    "Product_Active": True,
                }
                if opt.get("sku"):
                    wanted["Product_Code"] = opt["sku"]
                if (prod.get("brand") or {}).get("name"):
                    wanted["Brand"] = prod["brand"]["name"]
                wanted = {k: v for k, v in wanted.items() if v is not None}

                zp = self.prod_by_option.get(opt["id"])
                if zp is None:
                    candidates = zoho_by_key.get(mapping.product_key(info["Sub_Category"], info["Flavour"]), [])
                    if len(candidates) == 1:
                        zp = candidates.pop()
                    elif len(candidates) > 1:
                        self.report.add("Product", "needs review",
                                        f"{prod['name']}: {len(candidates)} Zoho products share sub-category+flavour")
                        continue
                if zp is None:
                    if not config.CREATE_NEW_PRODUCTS():
                        self.report.add("Product", "skipped", f"{prod['name']} not in Zoho (creation off)")
                        continue
                    name = info["display_name"]
                    if len(prod.get("options") or []) > 1 and opt.get("name"):
                        name = f"{name} ({opt['name']})"
                    # Zoho product names must be unique; Superbud sometimes lists the same item twice
                    for candidate in (name, prod.get("name"), f"{prod.get('name')} ({opt.get('name')})",
                                      f"{name} [{opt['id'][:8]}]"):
                        if candidate and mapping.norm(candidate) not in taken_names:
                            name = candidate
                            break
                    taken_names.add(mapping.norm(name))
                    record = dict(wanted, Product_Name=name)
                    for key in ("Product_Category", "Sub_Category", "Flavour"):
                        if info[key]:
                            record[key] = info[key]
                    desc = " | ".join(x for x in (opt.get("name"), prod.get("description")) if x)
                    if desc:
                        record["Description"] = desc[:2000]
                    creates.append((opt, record))
                    continue
                diff = _changed(zp, wanted)
                if diff:
                    updates.append((zp, opt, dict(diff, id=zp["id"])))
                else:
                    self.report.add("Product", "unchanged", zp["Product_Name"])

        # Options deleted in Superbud -> mark the Zoho product inactive (never deleted)
        for option_id, zp in self.prod_by_option.items():
            if option_id not in seen_options and zp.get("Product_Active") is not False:
                updates.append((zp, {"id": option_id}, {"id": zp["id"], "Product_Active": False}))

        for module_call, items, label in ((self.z.create, creates, "created"), (self.z.update, updates, "updated")):
            if not items:
                continue
            records = [i[-1] for i in items]
            results = module_call("Products", records)
            for item, res in zip(items, results):
                rec = item[-1]
                opt = item[1] if label == "updated" else item[0]
                ok = res.get("status") in ("success", "dry_run")
                name = rec.get("Product_Name") or (item[0].get("Product_Name") if label == "updated" else "")
                if ok:
                    if label == "created":
                        rec["id"] = res.get("details", {}).get("id")
                        self.prod_by_option[opt["id"]] = rec
                    else:
                        item[0].update(rec)
                        if opt.get("id") in seen_options:
                            self.prod_by_option[opt["id"]] = item[0]
                    fields = ", ".join(k for k in rec if k not in ("id",))
                    self.report.add("Product", label, f"{name} ({fields})" if label == "updated" else name)
                else:
                    self.report.add("Product", "error", f"{name}: {res}")

    # ============================================================== orders ==
    def fetch_orders(self):
        """Read orders first: they decide which new stores need a Dispensary this run."""
        since = (self.now - dt.timedelta(days=config.ORDER_LOOKBACK_DAYS())).isoformat().replace("+00:00", "Z")
        orders = {o["id"]: o for o in self.sb.orders_since(since)}
        # Also re-check older orders already in Zoho that are not finished yet
        open_ids = [oid for oid, d in self.deal_by_order.items()
                    if oid not in orders and d.get("Superbud_Order_Status") not in ("DELIVERED", "CANCELLED",
                                                                                       "REJECTED", "MERGED")]
        for i in range(0, len(open_ids), 50):
            for o in self.sb.orders_by_ids(open_ids[i:i + 50]):
                orders[o["id"]] = o
        # Orders cancelled/rejected before they ever reached Zoho (e.g. test orders) are ignored
        dead = {"CANCELLED", "REJECTED", "MERGED"}
        self.skipped_dead = [o for o in orders.values()
                             if o.get("status") in dead and o["id"] not in self.deal_by_order]
        for o in self.skipped_dead:
            orders.pop(o["id"])
        self.orders = orders
        self.active_locations = {
            (o.get("customerLocation") or {}).get("id") for o in orders.values()
            if o.get("status") != "DRAFT" or config.SYNC_DRAFT_ORDERS() or o["id"] in self.deal_by_order
        } - {None}

    def sync_orders(self):
        orders = self.orders
        if self.skipped_dead:
            self.report.add("Order", "skipped",
                            f"{len(self.skipped_dead)} orders cancelled/rejected before reaching Zoho (not created)")
        for order in sorted(orders.values(), key=lambda o: o.get("orderNumber") or 0):
            label = f"Order #{order.get('orderNumber')}"
            if order.get("status") == "DRAFT" and not config.SYNC_DRAFT_ORDERS() \
                    and order["id"] not in self.deal_by_order:
                self.report.add("Order", "skipped", f"{label}: still a draft")
                continue
            loc_id = (order.get("customerLocation") or {}).get("id")
            account = self.acc_by_loc.get(loc_id)
            if not account or not account.get("id"):
                self.report.add("Order", "needs review", f"{label}: its location is not linked to a Dispensary yet")
                continue
            try:
                contact_id = self.ensure_contact(order.get("buyerRep"), account, order)
                deal_id = self.upsert_deal(order, account, contact_id)
                won = mapping.deal_stage(order.get("status"), order.get("approvalState")) == "Closed Won"
                if (deal_id and won) or order["id"] in self.so_by_order:
                    self.upsert_sales_order(order, account, contact_id, deal_id)
            except ApiError as exc:
                self.report.add("Order", "error", f"{label}: {exc}")

    # -- contacts --
    def ensure_contact(self, buyer, account, order):
        if not buyer or not buyer.get("id"):
            return None
        # A buyer who is also our own sales rep is internal, not a customer contact
        if (order.get("salesRep") or {}).get("id") == buyer["id"]:
            return None
        email = (buyer.get("email") or "").lower()
        contact = self.contact_by_sb.get(buyer["id"]) or (self.contact_by_email.get(email) if email else None)
        wanted = {"Superbud_Buyer_User_ID": buyer["id"]}
        for key, value in (("First_Name", buyer.get("firstName")), ("Last_Name", buyer.get("lastName")),
                           ("Email", buyer.get("email")), ("Phone", buyer.get("phoneNumber"))):
            if value and (contact is None or not contact.get(key)):
                wanted[key] = value
        name = f"{buyer.get('firstName') or ''} {buyer.get('lastName') or ''}".strip() or email
        if contact is None:
            record = dict(wanted, Account_Name={"id": account["id"]})
            record.setdefault("Last_Name", buyer.get("lastName") or buyer.get("firstName") or email or "Unknown")
            new_id = first_id(self.z.create("Contacts", [record]))
            if not new_id:
                self.report.add("Contact", "error", name)
                return None
            record["id"] = new_id
            self.contact_by_sb[buyer["id"]] = record
            if email:
                self.contact_by_email[email] = record
            self.report.add("Contact", "created", f"{name} -> {account.get('Account_Name')}")
            return new_id
        diff = _changed(contact, wanted)
        if diff:
            self.z.update("Contacts", [dict(diff, id=contact["id"])])
            contact.update(diff)
            self.contact_by_sb[buyer["id"]] = contact
            self.report.add("Contact", "updated", f"{name} ({', '.join(diff)})")
        return contact["id"]

    # -- deals --
    def _deal_products(self, order):
        rows = []
        for line in order.get("lineItems") or []:
            option_id = ((line.get("option") or {}).get("currentModel") or {}).get("id")
            zp = self.prod_by_option.get(option_id) or {}
            rows.append({
                "Product_Category": zp.get("Product_Category"),
                "Sub_Category": zp.get("Sub_Category"),
                "Flavour": zp.get("Flavour"),
                "Quantities": str(line.get("quantity") or ""),
                "Sample_Size_Units": (line.get("option") or {}).get("name"),
            })
        return [{k: v for k, v in r.items() if v} for r in rows]

    def upsert_deal(self, order, account, contact_id):
        stage = mapping.deal_stage(order.get("status"), order.get("approvalState"))
        closing = (order.get("approvedAt") or order.get("createdAt") or self.now.isoformat())[:10]
        wanted = {
            "Deal_Name": mapping.deal_name(order),
            "Account_Name": {"id": account["id"]},
            "Amount": order.get("total"),
            "Stage": stage,
            "Closing_Date": closing,
            "Superbud_Order_ID": order["id"],
            "Superbud_Order_Number": str(order.get("orderNumber")),
            "Superbud_Order_Status": order.get("status"),
            "Sync_Source": "Superbud",
        }
        if contact_id:
            wanted["Contact_Name"] = {"id": contact_id}
        owner = self.owner(order.get("salesRep"))
        if owner:
            wanted["Owner"] = owner
        wanted = {k: v for k, v in wanted.items() if v is not None}
        deal = self.deal_by_order.get(order["id"])
        label = f"Order #{order.get('orderNumber')}"
        if deal is None:
            record = dict(wanted, Deal_Products=self._deal_products(order))
            new_id = first_id(self.z.create("Deals", [record]))
            if not new_id:
                self.report.add("Deal", "error", label)
                return None
            record["id"] = new_id
            self.deal_by_order[order["id"]] = record
            self.report.add("Deal", "created", f"{wanted['Deal_Name']} - {stage} - ${order.get('total')}")
            return new_id
        diff = _changed(deal, {k: v for k, v in wanted.items() if k != "Owner"})
        if diff:
            if "Amount" in diff or "Superbud_Order_Status" in diff:
                diff["Deal_Products"] = self._deal_products(order)
            self.z.update("Deals", [dict(diff, id=deal["id"])])
            deal.update(diff)
            changes = ", ".join(k for k in diff if k != "Deal_Products")
            self.report.add("Deal", "updated", f"{deal.get('Deal_Name')} ({changes})")
        else:
            self.report.add("Deal", "unchanged", deal.get("Deal_Name"))
        return deal["id"]

    # -- sales orders --
    def _ordered_items(self, order, existing_rows):
        """Line items for Zoho. Existing rows are matched by product so Zoho keeps row IDs;
        rows no longer in Superbud are deleted."""
        by_product = defaultdict(list)
        for row in existing_rows:
            pid = (row.get("Product_Name") or {}).get("id")
            by_product[pid].append(row)
        items, missing = [], []
        for line in order.get("lineItems") or []:
            option_id = ((line.get("option") or {}).get("currentModel") or {}).get("id")
            zp = self.prod_by_option.get(option_id)
            if not zp or not zp.get("id"):
                missing.append((line.get("option") or {}).get("name") or option_id)
                continue
            item = {
                "Product_Name": {"id": zp["id"]},
                "Quantity": line.get("quantity") or 0,
                "List_Price": line.get("basePriceDollars") if line.get("basePriceDollars") is not None
                else line.get("priceDollars"),
                "Discount": round(line.get("discountDollars") or 0, 2),
            }
            if by_product.get(zp["id"]):
                item["id"] = by_product[zp["id"]].pop(0)["id"]
            items.append(item)
        for rows in by_product.values():
            for row in rows:
                items.append({"id": row["id"], "_delete": None})
        return items, missing

    def upsert_sales_order(self, order, account, contact_id, deal_id):
        label = f"Order #{order.get('orderNumber')}"
        so = self.so_by_order.get(order["id"])
        if so and str(so.get("Superbud_Order_Version")) == str(order.get("version")):
            self.report.add("Sales Order", "unchanged", f"{label} (version {order.get('version')})")
            return
        existing_rows = []
        if so and so.get("id") and not str(so["id"]).startswith("DRY"):
            existing_rows = self.z.get_record("Sales_Orders", so["id"]).get("Ordered_Items") or []
        items, missing = self._ordered_items(order, existing_rows)
        if missing:
            self.report.add("Sales Order", "needs review",
                            f"{label}: products not linked in Zoho yet: {', '.join(map(str, missing))}")
            return
        adjustment = (order.get("shippingFee") or 0) - (order.get("credits") or 0) + (order.get("manualAdjustments") or 0)
        record = {
            "Subject": mapping.deal_name(order),
            "Status": mapping.sales_order_status(order.get("status")),
            "Account_Name": {"id": account["id"]},
            "Ordered_Items": items,
            "Tax": round((order.get("stateTax") or 0) + (order.get("localTax") or 0), 2),
            "Adjustment": round(adjustment, 2),
            "Superbud_Order_ID": order["id"],
            "Superbud_Order_Number": str(order.get("orderNumber")),
            "Superbud_Order_Version": order.get("version"),
            "Last_Superbud_Sync": self.now.isoformat(),
            "Sync_Source": "Superbud",
        }
        if deal_id:
            record["Deal_Name"] = {"id": deal_id}
        if contact_id:
            record["Contact_Name"] = {"id": contact_id}
        if order.get("dueDate"):
            record["Due_Date"] = order["dueDate"][:10]
        record.update(mapping.zoho_address(order.get("shippingAddress"), "Shipping"))
        if so is None:
            new_id = first_id(self.z.create("Sales_Orders", [record]))
            if new_id:
                record["id"] = new_id
                self.so_by_order[order["id"]] = record
                self.report.add("Sales Order", "created", f"{label}: {len(items)} line(s), ${order.get('total')}")
            else:
                self.report.add("Sales Order", "error", label)
        else:
            record["id"] = so["id"]
            old_version = so.get("Superbud_Order_Version")
            self.z.update("Sales_Orders", [record])
            so["Superbud_Order_Version"] = order.get("version")
            self.report.add("Sales Order", "updated", f"{label}: version {old_version} -> {order.get('version')}")

    # ================================================================= run ==
    def run(self):
        self.load_zoho()
        self.orders, self.active_locations, self.skipped_dead = {}, set(), []
        for step in (self.fetch_orders, self.sync_locations, self.sync_products, self.sync_orders):
            try:
                step()
            except ApiError as exc:
                self.report.add(step.__name__.replace("sync_", "").title(), "error", str(exc))
        return self.report
