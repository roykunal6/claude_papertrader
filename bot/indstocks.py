"""Small client for the INDstocks API.

Market data calls (quotes, candles, instruments) are read-only. The order calls
(place / modify / cancel) are used ONLY by bot/broker.py's LiveBroker, which runs
only when you start `python run.py live` with [live] enabled = true and type the
confirmation. `python run.py paper` never touches them.
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
        return self._request("GET", path, params=params, expect_json=expect_json)

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 expect_json: bool = True, retries: int = 5):
        for attempt in range(retries):
            with self._lock:
                wait = self._min_gap - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
            try:
                r = self.session.request(method, self.base_url + path, params=params, json=body, timeout=15)
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise ApiError(f"network error on {path}: {e}") from e
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429 or (r.status_code >= 500 and method == "GET"):
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

    # ---------- account (read-only) ----------
    def funds(self) -> dict:
        return self._get("/funds").get("data") or {}

    def positions(self) -> list[dict]:
        return self._get("/portfolio/positions", {"segment": "derivative"}).get("data") or []

    def order_book(self) -> list[dict]:
        return self._get("/order-book").get("data") or []

    def order(self, order_id: str) -> dict:
        return self._request("GET", "/order", body={"order_id": order_id, "segment": "DERIVATIVE"}).get("data") or {}

    # ---------- orders (REAL MONEY; used only by LiveBroker) ----------
    # One attempt only: never auto-retry an order call, or a timeout could place it twice.
    def place_order(self, txn_type: str, security_id: str, qty: int, limit_price: float, remarks: str) -> dict:
        return self._request("POST", "/order", retries=1, body={
            "txn_type": txn_type, "exchange": "NSE", "segment": "DERIVATIVE", "product": "INTRADAY",
            "order_type": "LIMIT", "validity": "DAY", "security_id": str(security_id), "qty": int(qty),
            "limit_price": float(limit_price), "is_amo": False, "remarks": remarks[:100]}).get("data") or {}

    def modify_order(self, order_id: str, qty: int, limit_price: float) -> dict:
        return self._request("POST", "/order/modify", retries=1, body={
            "order_id": order_id, "segment": "DERIVATIVE", "qty": int(qty),
            "limit_price": float(limit_price)}).get("data") or {}

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", "/order/cancel", retries=1,
                             body={"order_id": order_id, "segment": "DERIVATIVE"}).get("data") or {}

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


def _rows(body) -> list:
    """Pull the list out of {"status": "success", "data": [...]} or similar shapes."""
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        for k in ("expiries", "contracts", "items", "results", "data"):
            if isinstance(data.get(k), list):
                return data[k]
    return data if isinstance(data, list) else []


def expired_expiries(client: "IndstocksClient", underlying: str, start, end) -> list:
    """Past expiry dates (as date objects) between start and end (dates, span <= 1 year)."""
    from .instruments import parse_date
    body = client._get("/market/instruments/expired/expiries", {
        "underlying": underlying, "segment": "DERIVATIVE",
        "expiry_from": start.isoformat(), "expiry_to": end.isoformat()})
    out = []
    for v in _rows(body):
        v = v.get("expiry") if isinstance(v, dict) else v
        try:
            out.append(parse_date(str(v)[:10]))
        except ValueError:
            pass
    return sorted(set(out))


def expired_contracts(client: "IndstocksClient", underlying: str, expiry) -> list:
    """The full option chain of an expired expiry, as Contract objects.
    scrip is "EXP:<trading_symbol>" so it can't be mixed up with a live code."""
    from .instruments import Contract
    body = client._get("/market/instruments/expired/contracts", {
        "underlying": underlying, "segment": "DERIVATIVE", "expiry": expiry.isoformat()})
    out = []
    for r in _rows(body):
        try:
            if str(r.get("instrument_type", "OPTIDX")).upper() != "OPTIDX":
                continue
            sym = r["trading_symbol"]
            out.append(Contract(scrip=f"EXP:{sym}", symbol=sym, strike=float(r["strike_price"]),
                                opt_type=str(r["option_type"]).upper(), expiry=expiry,
                                lot_size=int(float(r.get("lot_size") or 65))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def expired_candles(client: "IndstocksClient", scrips: list, start_ms: int, end_ms: int,
                    interval: str = "1minute") -> dict:
    """Candles for expired contracts, keyed by the same "EXP:<symbol>" codes."""
    out = {}
    syms = [s.split(":", 1)[1] for s in scrips]
    for i in range(0, len(syms), 50):
        body = client._get(f"/market/historical/expired/{interval}", {
            "trading-symbols": ",".join(syms[i:i + 50]), "start_time": start_ms, "end_time": end_ms,
            "segment": "NFO"})
        for sym, v in (body.get("data") or {}).items():
            rows = (v or {}).get("candles") or [] if isinstance(v, dict) else v or []
            out[f"EXP:{sym}"] = [{"ts": int(c["ts"]), "o": float(c["o"]), "h": float(c["h"]),
                                  "l": float(c["l"]), "c": float(c["c"]), "v": float(c.get("v") or 0)}
                                 for c in rows]
    return out


def detect_option_prefix(client: "IndstocksClient", security_ids: list[str], log=print) -> str:
    """Work out whether option codes are NSE_<id> or NFO_<id>.

    Tries 1-min candles from the last 7 days (works on weekends too), then live quotes.
    A prefix INDstocks rejects outright (HTTP 400) is wrong; one it accepts but has no data
    for is the right format for a quiet contract.
    """
    end = int(time.time() * 1000)
    start = end - 7 * 86400 * 1000
    accepted = []
    for prefix in ("NSE", "NFO"):
        codes = [f"{prefix}_{sid}" for sid in security_ids[:5]]
        try:
            got = client.candles(codes, start, end)
        except ApiError as e:
            log(f"  {prefix}_ rejected for candles: {str(e)[:120]}")
            continue
        if any(got.get(c) for c in codes):
            return prefix
        accepted.append(prefix)
        try:
            q = client.ltp(codes)
            if any(v is not None for v in q.values()):
                return prefix
        except ApiError:
            pass
    if accepted:
        log(f"  No option data yet (market closed?). Using {accepted[0]}_, which INDstocks accepts.")
        return accepted[0]
    log("  Couldn't confirm the option code format. Falling back to NSE_.")
    return "NSE"


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
