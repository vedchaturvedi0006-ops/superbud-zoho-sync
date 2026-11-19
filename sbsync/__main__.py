"""Command line:

  python -m sbsync auth <grant_code>   one-time: save the Zoho refresh token
  python -m sbsync check               test both connections (reads only)
  python -m sbsync run                 run the sync (dry run unless LIVE=true in .env)
  python -m sbsync run --live          run and write to Zoho, this run only
  python -m sbsync schema Order        show the real fields of a Superbud type
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


def _err(exc):
    return type(exc).__name__ + " (details hidden in public logs)" if config.QUIET_OUTPUT() else str(exc)


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
            print(f"Sync crashed ({type(exc).__name__}). Run it on your PC to see the details.")
            return 1
        print(f"Sync crashed - details in {path}")
        raise
    finally:
        LOCK.unlink(missing_ok=True)


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
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
