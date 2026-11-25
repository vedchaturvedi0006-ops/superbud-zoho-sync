"""Dutchie POS API client (one API key = one store location).

Keys come from the environment only (.env on the PC, GitHub Secrets on GitHub):
  DUTCHIE_KEY_BENSON   Earth's Healing - Benson
  DUTCHIE_KEY_RIVER    Earth's Healing - River
"""
import datetime as dt

from . import config
from .http import ApiError, request

BASE = "https://api.pos.dutchie.com"

STORES = {
    "benson": ("Earth's Healing - Benson", "DUTCHIE_KEY_BENSON"),
    "river": ("Earth's Healing - River", "DUTCHIE_KEY_RIVER"),
}


def configured_stores():
    """[(code, label, Dutchie client)] for every store whose key is set."""
    out = []
    for code, (label, env) in STORES.items():
        key = config.get(env)
        if key:
            out.append((code, label, Dutchie(key)))
    return out


class Dutchie:
    def __init__(self, api_key):
        self._auth = (api_key, "")

    def _get(self, path, **params):
        params = {k: v for k, v in params.items() if v is not None}
        resp = request("GET", f"{BASE}{path}", auth=self._auth, params=params,
                       headers={"Accept": "application/json"}, timeout=180, min_interval=0.25)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {"raw": resp.text[:300]}
            raise ApiError(f"Dutchie GET {path} -> HTTP {resp.status_code}", resp.status_code, body)
        return resp.json() if resp.content else None

    def whoami(self):
        return self._get("/whoami")

    def transactions(self, start, end, detail=True):
        """Sales between two UTC datetimes (line items, taxes and order ids included)."""
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return self._get("/reporting/transactions", FromDateUTC=start.strftime(fmt), ToDateUTC=end.strftime(fmt),
                         IncludeDetail=str(detail).lower(), IncludeTaxes="true", IncludeOrderIds="true",
                         IncludeFeesAndDonations="true") or []

    def products(self):
        return self._get("/reporting/products") or []

    def inventory(self):
        return self._get("/reporting/inventory") or []

    def employees(self):
        return self._get("/employees") or []


def utc_days_ago(days):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return now - dt.timedelta(days=days), now
