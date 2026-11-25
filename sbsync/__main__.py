"""Command line:

  python -m sbsync auth <grant_code>   one-time: save the Zoho refresh token
  python -m sbsync check               test both connections (reads only)
  python -m sbsync run                 run the sync (dry run unless LIVE=true in .env)
  python -m sbsync run --live          run and write to Zoho, this run only
  python -m sbsync schema Order        show the real fields of a Superbud type
  python -m sbsync dutchie-check       test the Dutchie store keys (reads only)
  python -m sbsync retail              Dutchie sales -> Zoho "Retail Sales Stats" (this month)
  python -m sbsync retail --backfill   same, for all history Dutchie has
"""
import datetime as dt
import json
import os
import sys
import traceback

from . import config
from .http import ApiError

LOG_DIR = config.ROOT / "logs"
LOCK = config.ROOT / ".sync.lock"


def _log(text, name):
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{dt.datetime.now():%Y-%m-%d_%H%M%S}_{name}.txt"
    path.write_text(text, encoding="utf-8")
    # keep the newest 500 logs
    logs = sorted(LOG_DIR.glob("*.txt"))
    for old in logs[:-500]:
        old.unlink()
    return path


def cmd_auth(code):
    from .zoho import exchange_grant_code
    token = exchange_grant_code(code)
    config.save_env_value("ZOHO_REFRESH_TOKEN", token)
    print("Zoho refresh token saved to .env. Next: python -m sbsync check")


def _safe_code(exc):
    """Error type + HTTP status + API error code only - no record data (logs are public)."""
    parts = [type(exc).__name__]
    status = getattr(exc, "status", None)
    if status:
        parts.append(f"HTTP {status}")
    body = getattr(exc, "body", None)
    code = None
    if isinstance(body, dict):
        code = body.get("error") or body.get("code")
        errs = body.get("errors")
        if not code and isinstance(errs, list) and errs and isinstance(errs[0], dict):
            code = (errs[0].get("extensions") or {}).get("code")
    elif str(exc).startswith("SUPERBUD_API_KEY is missing"):
        code = "SUPERBUD_API_KEY missing"
    if code:
        parts.append(str(code)[:60])
    return ", ".join(parts)


def _err(exc):
    return _safe_code(exc) + " (details hidden in public logs)" if config.QUIET_OUTPUT() else str(exc)


def cmd_check():
    from .superbud import Superbud
    from .zoho import Zoho
    ok = True
    missing = [k for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN") if not config.get(k)]
    try:
        if missing:
            raise ApiError(f"{', '.join(missing)} empty in .env. Add the Self Client ID/secret, then run: "
                           "python -m sbsync auth <grant_code>")
        z = Zoho()
        accounts = z.coql_all("id, Account_Name", "Accounts")
        print(f"Zoho OK        - {len(accounts)} Dispensaries readable")
    except ApiError as exc:
        ok = False
        print(f"Zoho FAILED    - {_err(exc)}")
    try:
        sb = Superbud()
        did = sb.distributor_id()
        print(f"Superbud OK    - distributor {'found' if config.QUIET_OUTPUT() else 'id ' + did}")
        for label, fn in (("locations", sb.customer_locations), ("products", sb.products)):
            try:
                print(f"  {label:<10} {len(fn())} found")
            except ApiError as exc:
                ok = False
                print(f"  {label:<10} FAILED: {_err(exc)}")
        try:
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=config.ORDER_LOOKBACK_DAYS())) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"  {'orders':<10} {len(sb.orders_since(since))} found in the last {config.ORDER_LOOKBACK_DAYS()} days")
        except ApiError as exc:
            ok = False
            print(f"  {'orders':<10} FAILED: {_err(exc)}")
    except ApiError as exc:
        ok = False
        print(f"Superbud FAILED - {_err(exc)}")
    print("All checks passed." if ok else "Some checks failed - see above.")
    return 0 if ok else 1


def cmd_run(live_flag):
    from .superbud import Superbud
    from .sync import Sync
    from .zoho import Zoho
    live = live_flag or config.LIVE()
    if LOCK.exists() and (dt.datetime.now().timestamp() - LOCK.stat().st_mtime) < 3600:
        print("Another sync is still running (remove .sync.lock if that is wrong).")
        return 1
    LOCK.write_text(str(os.getpid()))
    try:
        zoho = Zoho(live=live)
        sync = Sync(zoho, Superbud(), live=live)
        report = sync.run()
        text = report.render(live)
        if not live:
            text += "\n\nWOULD WRITE TO ZOHO:\n" + json.dumps(zoho.writes, indent=2, default=str)
        path = _log(text, "live" if live else "dryrun")
        quiet = config.QUIET_OUTPUT()
        print(report.render(live, details=not quiet))
        if not quiet:
            print(f"\nFull report: {path}")
        return 1 if any(k.endswith(": error") for k in report.counts) else 0
    except Exception as exc:
        path = _log(traceback.format_exc(), "crash")
        if config.QUIET_OUTPUT():
            # error text can contain record data, so only the error type goes to public logs
            print(f"Sync crashed ({_safe_code(exc)}). Run it on your PC to see the details.")
            return 1
        print(f"Sync crashed - details in {path}")
        raise
    finally:
        LOCK.unlink(missing_ok=True)


def _fields(rows):
    keys = set()
    for row in rows[:200]:
        if isinstance(row, dict):
            keys |= set(row)
    return ", ".join(sorted(keys)) or "-"


def _how_many(n):
    """Exact counts on the PC; only a rough size in public GitHub logs."""
    if not config.QUIET_OUTPUT():
        return str(n)
    return "none" if n == 0 else ("some (under 100)" if n < 100 else "100 or more")


# Labels only (no names, emails or money) - safe to show in public logs
_ENUM_FIELDS = ("orderType", "orderSource", "transactionType", "isReturn", "isVoid", "isMedical",
                "customerTypeId", "category", "masterCategory", "productCategory", "unitWeightUnit")


def _labels(rows, field):
    vals = {str(r.get(field)) for r in rows if isinstance(r, dict) and r.get(field) not in (None, "")}
    return ", ".join(sorted(vals)[:25])


def cmd_dutchie_check():
    from .dutchie import STORES, configured_stores, utc_days_ago
    stores = configured_stores()
    if not stores:
        print("No Dutchie keys set. Add DUTCHIE_KEY_BENSON and DUTCHIE_KEY_RIVER as secrets.")
        return 1
    ok = True
    for code, label, d in stores:
        print(f"\n=== {label} ===")
        try:
            who = d.whoami() or {}
            name = str(who.get("locationName") or who.get("name") or "")
            expected = code in name.lower()
            print(f"  key works      yes (location {'matches' if expected else 'does NOT match'} '{code}')")
            print(f"  whoami fields  {_fields([who])}")
        except ApiError as exc:
            ok = False
            print(f"  key works      NO - {_err(exc)}")
            continue
        start, end = utc_days_ago(7)
        checks = (("transactions (7 days)", lambda: d.transactions(start, end)),
                  ("products", d.products), ("inventory", d.inventory), ("employees", d.employees))
        for name, fn in checks:
            try:
                rows = fn()
                print(f"  {name:<22} {_how_many(len(rows))}")
                print(f"    fields: {_fields(rows)}")
                items = [i for r in rows if isinstance(r, dict) for i in (r.get("items") or [])]
                if items:
                    print(f"    line item fields: {_fields(items)}")
                for f in _ENUM_FIELDS:
                    vals = _labels(rows, f)
                    if vals:
                        print(f"    {f}: {vals}")
            except ApiError as exc:
                ok = False
                print(f"  {name:<22} FAILED - {_err(exc)}")
    missing = [lbl for c, (lbl, env) in STORES.items() if not config.get(env)]
    if missing:
        print("\nNo key set for: " + ", ".join(missing))
    print("\nAll Dutchie checks passed." if ok else "\nSome Dutchie checks failed - see above.")
    return 0 if ok else 1


def cmd_retail(backfill):
    from .dutchie import configured_stores
    from .retail import RetailSync
    from .sync import Report
    from .zoho import Zoho
    live = config.LIVE()
    stores = configured_stores()
    if not stores:
        print("No Dutchie keys set (DUTCHIE_KEY_BENSON / DUTCHIE_KEY_RIVER).")
        return 1
    report = Report()
    zoho = Zoho(live=live)
    rs = RetailSync(zoho, report)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if backfill:
        start = dt.datetime(int(config.get("RETAIL_HISTORY_FROM_YEAR", "2018")), 1, 1, tzinfo=dt.timezone.utc)
    else:
        local_today = now.astimezone(rs.tz)
        first = local_today.replace(day=1, hour=0, minute=0, second=0)
        if local_today.day <= 3:  # late returns/edits for last month
            first = (first - dt.timedelta(days=1)).replace(day=1)
        start = first.astimezone(dt.timezone.utc)
    for code, label, client in stores:
        try:
            rs.sync_store(code, label, client, start, now, seen=set() if backfill else None)
        except ApiError as exc:
            report.add("Retail stats", "error", f"{label}: {exc}", code=_safe_code(exc))
    text = report.render(live)
    _log(text, "retail")
    print(report.render(live, details=not config.QUIET_OUTPUT()))
    return 1 if any(k.endswith(": error") for k in report.counts) else 0


def cmd_schema(type_name):
    from .superbud import Superbud
    print(json.dumps(Superbud().type_fields(type_name), indent=2))


def main(argv):
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "auth" and rest:
        return cmd_auth(rest[0]) or 0
    if cmd == "check":
        return cmd_check()
    if cmd == "run":
        return cmd_run("--live" in rest)
    if cmd == "schema" and rest:
        return cmd_schema(rest[0]) or 0
    if cmd == "dutchie-check":
        return cmd_dutchie_check()
    if cmd == "retail":
        return cmd_retail("--backfill" in rest)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
