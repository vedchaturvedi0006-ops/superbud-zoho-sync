"""Settings loaded from the .env file next to the project (no extra packages needed)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
# Optional non-secret settings (LIVE, CREATE_NEW_DISPENSARIES, ...). Loaded first, so it
# overrides the same lines in .env. Keys must stay in .env, never in this file.
SETTINGS_PATH = ROOT / "settings.env"


def _load_env(path=ENV_PATH):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(SETTINGS_PATH)
_load_env()


def get(name, default=None):
    return os.environ.get(name, default)


def flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def save_env_value(name, value, path=ENV_PATH):
    """Write or replace one KEY=value line in .env (used by the auth command)."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == name:
            out.append(f"{name}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{name}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[name] = value


# --- Zoho -------------------------------------------------------------------
ZOHO_CLIENT_ID = lambda: get("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = lambda: get("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = lambda: get("ZOHO_REFRESH_TOKEN")
ZOHO_ACCOUNTS_URL = lambda: get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")
ZOHO_API_DOMAIN = lambda: get("ZOHO_API_DOMAIN", "https://www.zohoapis.in")

# --- Superbud ---------------------------------------------------------------
SUPERBUD_API_URL = lambda: get("SUPERBUD_API_URL", "https://api.superbud.io/graphql")
SUPERBUD_API_KEY = lambda: get("SUPERBUD_API_KEY")
SUPERBUD_DISTRIBUTOR_ID = lambda: get("SUPERBUD_DISTRIBUTOR_ID")  # optional; found automatically

# --- Behaviour --------------------------------------------------------------
LIVE = lambda: flag("LIVE", False)                       # False = dry run, nothing is written
ORDER_LOOKBACK_DAYS = lambda: int(get("ORDER_LOOKBACK_DAYS", "120"))
# "on_activity" = create a Dispensary only when its Superbud store has an order (default)
# "true"        = create every Superbud store that is missing in Zoho
# "false"       = never create, only link existing Dispensaries
CREATE_NEW_DISPENSARIES = lambda: (get("CREATE_NEW_DISPENSARIES", "on_activity") or "on_activity").strip().lower()
CREATE_NEW_PRODUCTS = lambda: flag("CREATE_NEW_PRODUCTS", True)
SYNC_DRAFT_ORDERS = lambda: flag("SYNC_DRAFT_ORDERS", False)
PRODUCT_PRICE_MODE = lambda: get("PRODUCT_PRICE_MODE", "unit")  # "unit" (price / pack size) or "pack"
PUSH_LOCATIONS_TO_SUPERBUD = lambda: flag("PUSH_LOCATIONS_TO_SUPERBUD", False)
# On GitHub (public logs) print counts only - no names, emails or error details
QUIET_OUTPUT = lambda: flag("QUIET_OUTPUT", os.environ.get("GITHUB_ACTIONS") == "true")
# UTC hour of the daily "full pass" (license matching + creating new Dispensaries).
# Other runs only update records that are already linked, which keeps them fast.
FULL_PASS_HOUR_UTC = lambda: int(get("FULL_PASS_HOUR_UTC", "21"))  # 21:00 UTC = 02:30 IST
FORCE_FULL_PASS = lambda: flag("FORCE_FULL_PASS", False)
