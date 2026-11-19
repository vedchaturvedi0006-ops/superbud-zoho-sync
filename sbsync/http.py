"""HTTP helper with retries for rate limits (429) and temporary server errors."""
import random
import time

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def request(method, url, *, retries=5, min_interval=0.0, _last=[0.0], **kwargs):
    """Send a request; wait and retry on 429/5xx and network errors."""
    kwargs.setdefault("timeout", 60)
    for attempt in range(retries + 1):
        if min_interval:
            wait = _last[0] + min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            if attempt == retries:
                raise ApiError(f"Network error calling {url}: {exc}") from exc
            time.sleep(min(60, 2 ** attempt + random.random()))
            continue
        if resp.status_code in RETRY_STATUS and attempt < retries:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2 ** attempt
            time.sleep(min(120, delay + random.random()))
            continue
        return resp
    return resp
