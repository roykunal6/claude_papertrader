"""The trading rules, kept separate from data fetching so they can be tested and replayed.

Kunal's rules:
  * Nifty options only, nearest in-the-money strike, nearest weekly expiry
  * No trading on expiry day
  * No entry before 09:45; one trade per day; skip the day if there's no signal
  * Enter the moment the option's 1-min trend line flips to BLUE (BUY) with the
    candle closing above the line, and Nifty's own line agrees
    (blue for a CE, red for a PE)
  * Size: as many lots as fit inside max_invested (₹30,000)
  * Exit at +15% target, at the stop loss, on the red-line rule, or at 15:15
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .costs import order_charges
from .indicator import BLUE, RED, trend_line
from .instruments import Contract

IST = ZoneInfo("Asia/Kolkata")


def hhmm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


@dataclass
class Position:
    contract: Contract
    qty: int
    entry_price: float
    entry_time: datetime
    target: float
    stop: float
    entry_charges: float
    reason: str
    pending_exit: str | None = None  # live mode: an exit that hasn't fully filled yet
    peak: float = 0.0           # highest price since entry (for the trailing stop)
    initial_stop: float = 0.0


@dataclass
class Trade:
    date: str
    symbol: str
    opt_type: str
    strike: float
    qty: int
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_reason: str
    gross_pnl: float
    charges: float
    net_pnl: float
    entry_reason: str


@dataclass
class Strategy:
    cfg: dict
    log: callable = print
    trades_today: int = 0
    position: Position | None = None
    closed: list[Trade] = field(default_factory=list)

    # ---------------- helpers ----------------
    def _t(self, key: str) -> time:
        return hhmm(self.cfg["timing"][key])

    def _points(self, candles: list[dict]):
        ind = self.cfg["indicator"]
        return trend_line([c["c"] for c in candles], ind["length"], ind["method"])

    def size(self, price: float, lot_size: int) -> int:
        """Quantity (in units) that fits inside max_invested including buy charges,
        and below the exchange's per-order freeze limit."""
        budget = self.cfg["capital"]["max_invested"]
        lots = math.floor(budget / (price * lot_size))
        max_qty = self.cfg.get("live", {}).get("max_qty_per_order", 1755)
        lots = min(lots, max_qty // lot_size)
        while lots > 0:
            qty = lots * lot_size
            cost = price * qty + order_charges(price * qty, "BUY", self.cfg["charges"])
            if cost <= budget:
                return qty
            lots -= 1
        return 0

    def slip(self, price: float, side: str) -> float:
        s = self.cfg["paper"]["slippage_pct"] / 100
        p = price * (1 + s) if side == "BUY" else price * (1 - s)
        return round(round(p / 0.05) * 0.05, 2)  # option tick size ₹0.05

    # ---------------- decisions ----------------
    def can_enter(self, now: datetime) -> bool:
        t = now.astimezone(IST).time()
        return (self.position is None
                and self.trades_today < self.cfg["trade"]["max_trades_per_day"]
                and self._t("no_entry_before") <= t <= self._t("last_entry"))

    def entry_signal(self, now: datetime, index_candles: list[dict],
                     options: dict[str, tuple[Contract, list[dict]]]) -> tuple[Contract, str] | None:
        """Called once per closed 1-min candle. options = {"CE": (contract, candles), "PE": (...)}."""
        if not self.can_enter(now):
            return None
        idx_colour, idx_run = None, 0
        if index_candles:
            pts = self._points(index_candles)
            idx_colour = pts[-1].colour
            for pt in reversed(pts):  # how many candles has Nifty's line held this colour?
                if pt.colour != idx_colour:
                    break
                idx_run += 1
        for opt_type in ("CE", "PE"):
            if opt_type not in options:
                continue
            contract, candles = options[opt_type]
            if len(candles) < self.cfg["indicator"]["length"] + 2:
                continue
            last = self._points(candles)[-1]
            close = candles[-1]["c"]
            if last.signal != "BUY" or close <= last.line:
                continue
            if self.cfg["indicator"]["require_index_agreement"]:
                need = BLUE if opt_type == "CE" else RED
                min_run = self.cfg["indicator"].get("index_trend_min_candles", 0)
                if idx_colour != need or idx_run < min_run:
                    self.log(f"{now:%H:%M} {contract.symbol}: BUY on option chart but Nifty line is "
                             f"{idx_colour} for {idx_run} candle(s); needs {need} for {min_run}. Skipped.")
                    continue
            reason = (f"{opt_type} line flipped blue, close {close:.2f} above line {last.line:.2f}; "
                      f"Nifty line {idx_colour} for {idx_run} candles")
            return contract, reason
        return None

    def red_line_exit(self, candles: list[dict]) -> bool:
        if not (self.position and self.cfg["trade"]["exit_on_red_line"] and candles):
            return False
        last = self._points(candles)[-1]
        return last.colour == RED and candles[-1]["c"] < last.line

    # ---------------- paper fills ----------------
    def open(self, contract: Contract, market_price: float, now: datetime, reason: str,
             filled_qty: int | None = None) -> Position | None:
        """Paper: fill at last price plus slippage. Live: pass the real fill price and filled_qty."""
        live = filled_qty is not None
        price = market_price if live else self.slip(market_price, "BUY")
        qty = filled_qty if live else self.size(price, contract.lot_size)
        if qty == 0:
            self.log(f"{now:%H:%M} Signal on {contract.symbol} but 1 lot at ₹{price} "
                     f"(x{contract.lot_size}) is more than ₹{self.cfg['capital']['max_invested']}. No trade.")
            self.trades_today += 1  # the day's one signal is used up; don't chase
            return None
        tr = self.cfg["trade"]
        self.position = Position(
            contract=contract, qty=qty, entry_price=price, entry_time=now,
            target=round(round(price * (1 + tr["target_pct"] / 100) / 0.05) * 0.05, 2),
            stop=round(round(price * (1 - tr["stop_loss_pct"] / 100) / 0.05) * 0.05, 2),
            entry_charges=order_charges(price * qty, "BUY", self.cfg["charges"]),
            reason=reason)
        self.position.peak = price
        self.position.initial_stop = self.position.stop
        self.trades_today += 1
        p = self.position
        self.log(f"{now:%H:%M:%S} {'LIVE' if live else 'PAPER'} BUY {qty} x {contract.symbol} @ ₹{price} "
                 f"(₹{price * qty:,.0f}). Target ₹{p.target}, stop ₹{p.stop}. Why: {reason}")
        return p

    def update_trail(self, price: float) -> str | None:
        """Raise the stop as the option rises. Never lowers it. Returns a message if it moved.

        Conservative trailing rule (config [trail]):
          * up breakeven_at_pct  -> stop moves to the entry price (can't lose on the trade)
          * after that           -> stop follows trail_pct below the highest price seen
          * past the target      -> (target_mode = "trail") the gap tightens to tight_trail_pct
        """
        p = self.position
        tr = self.cfg.get("trail", {})
        if not p or not tr.get("enabled", False):
            return None
        p.peak = max(p.peak, price)
        gain = (p.peak / p.entry_price - 1) * 100
        new_stop = p.stop
        if gain >= tr.get("breakeven_at_pct", 5):
            new_stop = max(new_stop, p.entry_price)
            gap = tr.get("trail_pct", 7)
            if gain >= self.cfg["trade"]["target_pct"] and tr.get("target_mode", "exit") == "trail":
                gap = tr.get("tight_trail_pct", 4)
            new_stop = max(new_stop, p.peak * (1 - gap / 100))
        new_stop = round(round(new_stop / 0.05) * 0.05, 2)
        if new_stop > p.stop + 0.001:
            old, p.stop = p.stop, new_stop
            return f"Stop raised ₹{old} -> ₹{new_stop} (high so far ₹{p.peak:.2f}, up {gain:.1f}%)"
        return None

    def check_price(self, ltp: float) -> str | None:
        """Exit reason if the live price has hit the target or stop, else None."""
        p = self.position
        if not p:
            return None
        trail_on = self.cfg.get("trail", {}).get("enabled", False)
        target_exits = not trail_on or self.cfg["trail"].get("target_mode", "exit") == "exit"
        if target_exits and ltp >= p.target:
            return "TARGET"
        if ltp <= p.stop:
            return "TRAIL_STOP" if p.stop > p.initial_stop else "STOP_LOSS"
        return None

    def close(self, market_price: float, now: datetime, reason: str, exact: bool = False,
              live: bool = False) -> Trade:
        p = self.position
        assert p is not None
        price = market_price if exact else self.slip(market_price, "SELL")
        exit_charges = order_charges(price * p.qty, "SELL", self.cfg["charges"])
        gross = (price - p.entry_price) * p.qty
        charges = p.entry_charges + exit_charges
        trade = Trade(
            date=f"{p.entry_time.astimezone(IST):%Y-%m-%d}", symbol=p.contract.symbol,
            opt_type=p.contract.opt_type, strike=p.contract.strike, qty=p.qty,
            entry_time=f"{p.entry_time.astimezone(IST):%H:%M:%S}", entry_price=p.entry_price,
            exit_time=f"{now.astimezone(IST):%H:%M:%S}", exit_price=price, exit_reason=reason,
            gross_pnl=round(gross, 2), charges=round(charges, 2), net_pnl=round(gross - charges, 2),
            entry_reason=p.reason)
        self.closed.append(trade)
        self.position = None
        self.log(f"{now:%H:%M:%S} {'LIVE' if live else 'PAPER'} SELL {trade.qty} x {trade.symbol} @ ₹{price} ({reason}). "
                 f"P&L ₹{trade.gross_pnl:,.2f} gross, ₹{trade.net_pnl:,.2f} after ₹{trade.charges:,.2f} charges")
        return trade
