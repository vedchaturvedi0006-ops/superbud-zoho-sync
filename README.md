# Superbud → Zoho CRM sync

Keeps Zoho CRM in step with Superbud (Nature's Medicines distributor account).
It runs on GitHub Actions every 15 minutes (or on your PC with `schedule.ps1`). Each run reads Superbud and Zoho, changes only
what differs, and saves a report in `logs/`.

## What syncs

| Superbud | → Zoho | Matched by |
|---|---|---|
| Customer Location (store) | Dispensary | `Superbud_Customer_Location_ID`, then **license number** (first time) |
| Purchaser on an order | Contact | `Superbud_Buyer_User_ID`, then **email** |
| Product option | Product | `Superbud_Product_Option_ID`, then **Sub Category + Flavour** |
| Order (placed, not draft) | Deal | `Superbud_Order_ID` |
| Order once confirmed/approved | Sales Order + line items | `Superbud_Order_ID` + order version |

**Rules**
- **Dispensary.** Existing names and licenses are never overwritten. Empty address fields get filled in.
  - A new Dispensary gets the company name. If the company has several stores, it's "Company - Store".
  - New ones are tagged `Superbud_Sync_Status = Created by Sync`. Name-only matches are tagged `Needs Review`.
- **Products.** Price, stock, brand and active state follow Superbud.
  - Price is per unit (pack price ÷ pack size), the same as your current Zoho prices.
  - A product removed in Superbud is set inactive, never deleted.
  - `product_map.json` translates Superbud collections to your Zoho categories. Edit it when Superbud adds a new product line.
- **Deal Stage.**
  - Draft or in review → `open`.
  - Confirmed, packed, shipped, delivered, or approved → `Closed Won`.
  - Cancelled, rejected or merged → `Closed Lost`.
  - The exact Superbud status is kept in `Superbud_Order_Status`.
- **Sales Order.** It's created once the Deal is Closed Won.
  - It's updated whenever Superbud's order version changes: quantity and price changes, added lines, removed lines.
  - Status: Created → Approved (confirmed or packed) → Delivered (shipped or delivered) → Cancelled.
- **Zoho → Superbud (optional, off by default).** With `PUSH_LOCATIONS_TO_SUPERBUD=true`, Dispensary shipping address changes are sent to Superbud.
  - Superbud's API currently can't change order status, payments or sales rep, so those stay one-way.
- **No duplicates.** Every Zoho record stores its Superbud ID, so re-running is always safe. A second run with no changes writes nothing.
- **Not touched.** Records that didn't come from Superbud (Market Visits, your own Deals) are left alone.

## Setup (once)

1. **Superbud API key.** Ask your Superbud account manager for a new key, and ask them to revoke the old one that was shared in a chat.
2. **Zoho API client.** Go to https://api-console.zoho.in → *Add Client* → **Self Client** → *Create*.
   - Copy the Client ID and Client Secret into `.env` yourself.
3. **Install.** In PowerShell, in this folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   notepad .env        # fill in ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, SUPERBUD_API_KEY
   ```
4. **Connect Zoho.** In the Self Client, open the *Generate Code* tab.
   - Scope: `ZohoCRM.modules.ALL,ZohoCRM.settings.READ,ZohoCRM.coql.READ,ZohoCRM.users.READ`
   - Time duration: 10 minutes. Click *Create*, then run straight away:
   ```powershell
   .\.venv\Scripts\python.exe -m sbsync auth PASTE_THE_CODE_HERE
   ```
5. **Test the connections** (reads only):
   ```powershell
   .\.venv\Scripts\python.exe -m sbsync check
   ```
6. **Dry run.** Nothing is written:
   ```powershell
   .\.venv\Scripts\python.exe -m sbsync run
   ```
   Open the newest file in `logs\`. It lists every Dispensary, Product, Deal and Sales Order that **would** be created or changed.
7. **Go live.** When the report looks right, set `LIVE=true` in `.env`, then:
   ```powershell
   .\.venv\Scripts\python.exe -m sbsync run
   powershell -ExecutionPolicy Bypass -File .\schedule.ps1
   ```
   The sync now runs every 15 minutes while you're logged in to the PC. To stop it, run `.\schedule.ps1 -Remove`.

## Running on GitHub Actions (every 15 minutes, PC not needed)

The workflow is `.github/workflows/sync.yml`. In the GitHub repo, go to **Settings → Secrets and variables → Actions**.

**Secrets** (type these yourself; GitHub keeps them encrypted and never shows them in logs):
`ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `SUPERBUD_API_KEY`. Copy the values from your `.env`.

**Variables** (settings, not secret):
| Variable | Default | Meaning |
|---|---|---|
| `LIVE` | `false` | `true` = write to Zoho. Keep `false` until a dry run looks right |
| `CREATE_NEW_DISPENSARIES` | `on_activity` | `on_activity` = create a missing store's Dispensary when it places an order; `true` = create all missing stores; `false` = never |
| `CREATE_NEW_PRODUCTS` | `true` | create Products that aren't in Zoho yet |

**How the runs work**
- Every 15 minutes: links, prices, stock, Deals and Sales Orders are kept up to date.
- Once a day, in the run around 02:30 IST: the "full pass" matches new Superbud stores to Dispensaries by license, and creates new ones if that's turned on.
  - Run a full pass any time with **Actions → Superbud to Zoho sync → Run workflow**, ticking *full pass*.
- **The repo is public, so the logs only show counts**, never names or emails. For the detailed report, run `python -m sbsync run` on your PC. That's a dry run unless `LIVE=true` in your `.env`.
- GitHub pauses scheduled workflows in public repos after 60 days with no commits. If that happens, open **Actions** and click **Enable workflow**.

## If something fails

- `python -m sbsync check` shows which side and which query failed.
- **Superbud field errors** (e.g. `Cannot query field "x"`): the Superbud API is in beta and may rename fields.
  - All queries are at the top of `sbsync/superbud.py`.
  - `python -m sbsync schema Order` lists the real field names of a type.
- **Needs Review lines** in the report are records the sync wouldn't guess about. Fix them in Zoho, for example by putting the right `Superbud_Customer_Location_ID` on a Dispensary, and the next run picks them up.
- **Tests:** `python -m unittest discover tests` runs the offline tests with fake data.
