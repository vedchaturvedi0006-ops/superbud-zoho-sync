"""Small Zoho CRM v8 client: token refresh, COQL reads, create/update in batches."""
import time

from . import config
from .http import ApiError, request

SCOPES = "ZohoCRM.modules.ALL,ZohoCRM.settings.READ,ZohoCRM.coql.READ,ZohoCRM.users.READ"


def exchange_grant_code(code):
    """One-time setup: turn a Self Client grant code into a refresh token."""
    resp = request("POST", f"{config.ZOHO_ACCOUNTS_URL()}/oauth/v2/token", data={
        "grant_type": "authorization_code",
        "client_id": config.ZOHO_CLIENT_ID(),
        "client_secret": config.ZOHO_CLIENT_SECRET(),
        "code": code,
    })
    data = resp.json()
    if "refresh_token" not in data:
        raise ApiError(f"Zoho did not return a refresh token: {data}", resp.status_code, data)
    return data["refresh_token"]


class Zoho:
    def __init__(self, live=False):
        self.live = live
        self._token = None
        self._token_expiry = 0
        self.writes = []  # what was (or would be) written, for the report

    # -- auth ---------------------------------------------------------------
    def _access_token(self):
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = request("POST", f"{config.ZOHO_ACCOUNTS_URL()}/oauth/v2/token", data={
            "grant_type": "refresh_token",
            "client_id": config.ZOHO_CLIENT_ID(),
            "client_secret": config.ZOHO_CLIENT_SECRET(),
            "refresh_token": config.ZOHO_REFRESH_TOKEN(),
        })
        data = resp.json()
        if "access_token" not in data:
            hint = ""
            if data.get("error") == "invalid_client":
                hint = (" - the Client ID/Secret in .env don't match a Zoho client on accounts.zoho.in "
                        "(create the Self Client at api-console.zoho.in and copy both values again)")
            elif data.get("error") == "invalid_code":
                hint = " - the refresh token is wrong or revoked; run: python -m sbsync auth <new_grant_code>"
            raise ApiError(f"Zoho token refresh failed: {data}{hint}", resp.status_code, data)
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _call(self, method, path, **kwargs):
        for attempt in range(3):
            headers = {"Authorization": f"Zoho-oauthtoken {self._access_token()}"}
            resp = request(method, f"{config.ZOHO_API_DOMAIN()}/crm/v8{path}", headers=headers, **kwargs)
            # Zoho drops older access tokens once too many are issued from the same refresh token
            # (the 2-minute Superbud sync shares it), so long runs can lose their token: get a new one.
            if resp.status_code == 401 and attempt < 2:
                self._token = None
                time.sleep(2)
                continue
            break
        if resp.status_code == 204:
            return {}
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}
        if resp.status_code >= 400:
            raise ApiError(f"Zoho {method} {path} -> HTTP {resp.status_code}: {body}", resp.status_code, body)
        return body

    # -- reads --------------------------------------------------------------
    def coql_all(self, select, module, where="id is not null"):
        """Read every record matching a COQL query (pages of 200)."""
        rows, offset = [], 0
        while True:
            query = f"select {select} from {module} where {where} limit {offset}, 200"
            body = self._call("POST", "/coql", json={"select_query": query})
            batch = body.get("data", [])
            rows.extend(batch)
            if not body.get("info", {}).get("more_records"):
                return rows
            offset += 200

    def get_record(self, module, record_id):
        body = self._call("GET", f"/{module}/{record_id}")
        return (body.get("data") or [{}])[0]

    def users(self):
        body = self._call("GET", "/users", params={"type": "ActiveUsers"})
        return body.get("users", [])

    # -- writes -------------------------------------------------------------
    def _write(self, method, module, records, label):
        results = []
        for rec in records:
            self.writes.append({"action": label, "module": module, "record": rec})
        if not self.live:
            return [{"status": "dry_run", "details": {"id": r.get("id", "DRY-RUN")}} for r in records]
        for i in range(0, len(records), 100):
            chunk = records[i:i + 100]
            try:
                body = self._call(method, f"/{module}", json={"data": chunk, "trigger": []})
            except ApiError as exc:
                # Zoho answers HTTP 400 when every record in the batch is rejected; the
                # per-record reasons are still in the body, so report them record by record.
                if isinstance(exc.body, dict) and isinstance(exc.body.get("data"), list):
                    body = exc.body
                else:
                    raise
            results.extend(body.get("data", []))
        return results

    def delete(self, module, ids):
        """Delete up to 100 records by id."""
        ids = [str(i) for i in ids][:100]
        self.writes.append({"action": "delete", "module": module, "record": {"ids": len(ids)}})
        if not self.live or not ids:
            return []
        body = self._call("DELETE", f"/{module}", params={"ids": ",".join(ids)})
        return body.get("data", [])

    def create(self, module, records):
        return self._write("POST", module, records, "create")

    def update(self, module, records):
        return self._write("PUT", module, records, "update")


def first_id(results):
    """ID of the first created record, or None if Zoho reported an error."""
    if not results:
        return None
    res = results[0]
    if res.get("status") in ("success", "dry_run"):
        return res.get("details", {}).get("id")
    return None
