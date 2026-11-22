"""Real order placement on INDstocks. REAL MONEY.

Used only by `python run.py live` when config.toml has [live] enabled = true and
you type the confirmation that morning. `python run.py paper` never imports this.

How an order is placed:
  * Always a LIMIT order (INDstocks turns MARKET into LIMIT anyway), priced a little
    through the last price (buy a bit above, sell a bit below) so it fills at once.
  * Product INTRADAY, segment DERIVATIVE, exchange NSE.
  * Order calls are never retried automatically, so one signal can't buy twice.
  * After placing, it checks the order every second until it's filled, rejected or timed out.
  * Buy not filled in time: cancel it, keep whatever part did fill.
  * Sell not filled: re-price lower and try again; if it still fails, alert you to sell by hand.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass

from .indstocks import ApiError

FILLED = {"COMPLETE", "COMPLETED", "EXECUTED", "FILLED", "TRADED", "SUCCESS"}
DEAD = {"REJECTED", "CANCELLED", "CANCELED", "FAILED", "EXPIRED", "LAPSED"}
TAG = "nifty-bot"


def tick(price: float) -> float:
    return round(max(0.05, round(price / 0.05) * 0.05), 2)


@dataclass
class Fill:
    qty: int
    price: float
    order_id: str
    note: str = ""


def security_id(scrip: str) -> str:
    """NFO_64489 -> 64489"""
    return scrip.split("_", 1)[1] if "_" in scrip else scrip


class LiveBroker:
    def __init__(self, client, cfg: dict, log=print, sleep=_time.sleep):
        self.c = client
        lv = cfg.get("live", {})
        self.buffer = lv.get("limit_buffer_pct", 1.0) / 100
        self.wait_s = lv.get("fill_wait_seconds", 10)
        self.log = log
        self.sleep = sleep

    # ---------- checks ----------
    def option_buy_funds(self) -> float | None:
        f = self.c.funds()
        v = (f.get("detailed_avl_balance") or {}).get("option_buy")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def open_nifty_positions(self) -> list[dict]:
        return [p for p in self.c.positions()
                if "NIFTY" in str(p.get("symbol", "")).upper() and float(p.get("net_qty") or 0) != 0]

    def bot_orders_today(self) -> list[dict]:
        return [o for o in self.c.order_book() if TAG in str(o.get("remarks", ""))]

    # ---------- orders ----------
    def _wait(self, order_id: str) -> tuple[str, int, float, str]:
        status, qty, price, info = "UNKNOWN", 0, 0.0, ""
        for _ in range(int(self.wait_s)):
            self.sleep(1)
            try:
                o = self.c.order(order_id)
            except ApiError as e:
                self.log(f"Order {order_id}: status check failed ({e}). Trying again.")
                continue
            status = str(o.get("status") or o.get("order_status") or "").upper()
            qty = int(float(o.get("traded_qty") or 0))
            price = float(o.get("traded_price") or 0)
            info = str(o.get("extra_info") or "")
            if status in FILLED or status in DEAD:
                break
        return status, qty, price, info

    def _place(self, side: str, contract, qty: int, ltp: float) -> tuple[str | None, str]:
        price = tick(ltp * (1 + self.buffer) if side == "BUY" else ltp * (1 - self.buffer))
        try:
            r = self.c.place_order(side, security_id(contract.scrip), qty, price, f"{TAG} {side.lower()}")
        except ApiError as e:
            return None, str(e)
        oid = r.get("order_id")
        self.log(f"LIVE {side} order sent: {qty} x {contract.symbol} limit ₹{price}. Order id {oid}.")
        return oid, ""

    def buy(self, contract, qty: int, ltp: float) -> Fill:
        oid, err = self._place("BUY", contract, qty, ltp)
        if not oid:
            return Fill(0, 0.0, "", f"Buy order was not accepted: {err}")
        status, got, price, info = self._wait(oid)
        if status not in FILLED:
            try:
                self.c.cancel_order(oid)
            except ApiError:
                pass
            status, got, price, info = self._wait_once(oid, status, got, price, info)
        note = "" if got == qty else f"Buy {status.lower() or 'not filled'}: {got} of {qty} filled. {info}".strip()
        return Fill(got, price, oid, note)

    def _wait_once(self, oid, status, got, price, info):
        try:
            o = self.c.order(oid)
            return (str(o.get("status") or status).upper(), int(float(o.get("traded_qty") or got)),
                    float(o.get("traded_price") or price), str(o.get("extra_info") or info))
        except ApiError:
            return status, got, price, info

    def sell(self, contract, qty: int, ltp: float, get_ltp=None) -> Fill:
        """Sell qty. Re-prices up to 3 times. Returns what was sold."""
        sold, value, note, last_oid = 0, 0.0, "", ""
        for attempt in range(3):
            left = qty - sold
            if left <= 0:
                break
            if attempt and get_ltp:
                ltp = get_ltp() or ltp
            price = tick(ltp * (1 - self.buffer * (1 + attempt)))  # go further through the price each try
            try:
                r = self.c.place_order("SELL", security_id(contract.scrip), left, price, f"{TAG} sell")
            except ApiError as e:
                note = f"Sell order was not accepted: {e}"
                self.sleep(1)
                continue
            last_oid = r.get("order_id") or ""
            self.log(f"LIVE SELL order sent: {left} x {contract.symbol} limit ₹{price}. Order id {last_oid}.")
            status, got, fill_price, info = self._wait(last_oid)
            if status not in FILLED:
                try:
                    self.c.cancel_order(last_oid)
                except ApiError:
                    pass
                status, got, fill_price, info = self._wait_once(last_oid, status, got, fill_price, info)
            sold += got
            value += got * fill_price
            if got < left:
                note = f"Sell {status.lower() or 'not filled'}: {got} of {left} filled. {info}".strip()
        avg = round(value / sold, 2) if sold else 0.0
        return Fill(sold, avg, last_oid, "" if sold == qty else note)
