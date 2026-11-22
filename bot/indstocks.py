"""Small read-only client for the INDstocks API.

This bot only READS data (quotes, candles, instruments). It has no code that
places, modifies or cancels orders, so it cannot trade real money.
"""
from __future__ import annotations

import os
import threading
import time

import requests


class ApiError(RuntimeError):
    pass


class IndstocksClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": token, "Accept": "application/json"})
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.last_errors: dict = {}  # per-instrument errors from the last quote/candle call
        self._min_gap = 0.4  # stay well under the 5 requests/second limit

    # ---------- auth ----------
    @classmethod
    def from_env(cls, base_url: str) -> "IndstocksClient":
        token = os.environ.get("INDSTOCKS_ACCESS_TOKEN", "").strip()
        if not token:
            token = generate_token_from_totp(base_url)
        return cls(base_url, token)

    # ---------- plumbing ----------
    def _get(self, path: str, params: dict | None = None, expect_json: bool = True):
        for attempt in range(5):
            with self._lock:
                wait = self._min_gap - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
            try:
                r = self.session.get(self.base_url + path, params=params, timeout=15)
            except requests.RequestException as e:
                if attempt == 4:
                    raise ApiError(f"network error on {path}: {e}") from e
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 1.5 * (attempt + 1)
                time.sleep(max(wait, 1.0))
                continue
            if r.status_code in (401, 403):
                raise ApiError(f"{path}: token rejected ({r.status_code}). Refresh INDSTOCKS_ACCESS_TOKEN. {r.text[:200]}")
            if r.status_code >= 400:
                raise ApiError(f"{path}: HTTP {r.status_code}: {r.text[:300]}")
            return r.json() if expect_json else r.text
        raise ApiError(f"{path}: gave up after retries")

    # ---------- endpoints ----------
    def profile(self) -> dict:
        return self._get("/user/profile")

    def fno_instruments_csv(self) -> str:
        return self._get("/market/instruments", {"source": "fno"}, expect_json=False)

    def ltp(self, scrips: list[str]) -> dict[str, float]:
        body = self._get("/market/quotes/ltp", {"scrip-codes": ",".join(scrips)})
        self.last_errors = body.get("errors") or {}
        data = body.get("data") or {}
        out = {}
        for code, v in data.items():
            price = v.get("live_price") if isinstance(v, dict) else v
            if price is not None:
                out[code] = float(str(price).replace(",", ""))
        return out

    def candles(self, scrips: list[str], start_ms: int, end_ms: int, interval: str = "1minute") -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for i in range(0, len(scrips), 5):  # max 5 scrips per call
            chunk = scrips[i:i + 5]
            body = self._get(f"/market/historical/{interval}", {
                "scrip-codes": ",".join(chunk), "start_time": start_ms, "end_time": end_ms})
            self.last_errors = body.get("errors") or {}
            for code, v in (body.get("data") or {}).items():
                rows = (v or {}).get("candles") or []
                out[code] = [{"ts": int(c["ts"]), "o": float(c["o"]), "h": float(c["h"]),
                              "l": float(c["l"]), "c": float(c["c"]), "v": float(c.get("v") or 0)}
                             for c in rows]
        return out


def detect_option_prefix(client: "IndstocksClient", security_id: str) -> str:
    """Options may be quoted as NFO_<id> or NSE_<id>; try both and keep the one that answers."""
    for prefix in ("NFO", "NSE"):
        code = f"{prefix}_{security_id}"
        try:
            if client.ltp([code]).get(code) is not None:
                return prefix
        except ApiError:
            pass
    raise ApiError(f"Couldn't get a quote for option {security_id} as NFO_ or NSE_. Last errors: {client.last_errors}")


def generate_token_from_totp(base_url: str) -> str:
    """Optional: make a fresh 24-hour token from Client ID + MPIN + TOTP secret in .env."""
    client_id = os.environ.get("INDSTOCKS_CLIENT_ID", "").strip()
    mpin = os.environ.get("INDSTOCKS_MPIN", "").strip()
    secret = os.environ.get("INDSTOCKS_TOTP_SECRET", "").strip()
    if not (client_id and mpin and secret):
        raise ApiError("No token. Put INDSTOCKS_ACCESS_TOKEN in .env "
                       "(or INDSTOCKS_CLIENT_ID + INDSTOCKS_MPIN + INDSTOCKS_TOTP_SECRET).")
    import pyotp  # only needed for this path
    r = requests.post(base_url.rstrip("/") + "/generate/token",
                      headers={"x-api-key": client_id, "Content-Type": "application/json"},
                      json={"mpin": mpin, "totp": pyotp.TOTP(secret).now()}, timeout=15)
    if r.status_code != 200:
        raise ApiError(f"token generation failed: HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    token = body.get("token") or (body.get("data") or {}).get("token")
    if not token:
        raise ApiError(f"token generation: no token in response: {str(body)[:200]}")
    return token
