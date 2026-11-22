"""The trading rules, kept separate from data fetching so they can be tested and replayed.

Strategy: Opening Range Breakout on Nifty (see bot/orb.py for the plain-English version).
  * Nifty options only, in-the-money strike at the moment of entry, nearest weekly expiry
  * No trading on expiry day; one trade per day; skip the day if there's no clean breakout
  * Entry and exits are decided on the Nifty index's own chart, not the option's
  * Size: 1+ lots, never more than max_invested (₹30,000) and never more than max_loss_per_trade at risk
  * Exit at the Nifty target, the Nifty stop, the option's safety stop, or at 15:15
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .costs import order_charges
from .instruments import Contract
from .orb import IST, Setup, find_setup, hhmm, spot_exit

__all__ = ["IST", "Position", "Strategy", "Trade", "hhmm"]


def tick(price: float) -> float:
    return round(round(price / 0.05) * 0.05, 2)  # option tick size ₹0.05


@dataclass
class Position:
    contract: Contract
    qty: int
    entry_price: float
    entry_time: datetime
    target: float               # rough option price at the Nifty target (for the alert only)
    stop: float                 # option safety stop (exits if the option itself falls this far)
    entry_charges: float
    reason: str
    pending_exit: str | None = None  # live mode: an exit that hasn't fully filled yet
    peak: float = 0.0           # highest option price since entry
    initial_stop: float = 0.0
    spot_entry: float = 0.0     # Nifty level at entry
    spot_stop: float = 0.0      # Nifty level that ends the trade
    spot_target: float = 0.0    # Nifty level that books the profit
    risk_pts: float = 0.0
    spot_stop_moved: bool = False


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
    _notes: set = field(default_factory=set)
    _seen_bars: set = field(default_factory=set)

    def _t(self, key: str):
        return hhmm(self.cfg["timing"][key])

    def note(self, msg: str) -> None:
        """Log a skip reason once per day."""
        if msg not in self._notes:
            self._notes.add(msg)
            self.log(msg)

    # ---------------- sizing ----------------
    def size(self, price: float, lot_size: int, risk_pts: float | None = None) -> int:
        """Quantity (units) that fits inside max_invested including buy charges, keeps the estimated loss
        at the stop under max_loss_per_trade, and stays below the exchange's per-order freeze limit."""
        tr = self.cfg["trade"]
        budget = self.cfg["capital"]["max_invested"]
        lots = math.floor(budget / (price * lot_size))
        lots = min(lots, self.cfg.get("live", {}).get("max_qty_per_order", 1755) // lot_size)
        lots = min(lots, tr.get("max_lots", 99))
        max_loss = tr.get("max_loss_per_trade")
        if max_loss and risk_pts:
            delta = self.cfg["orb"].get("delta_guess", 0.6)
            per_unit = min(risk_pts * delta, price * tr["stop_loss_pct"] / 100)
            per_unit += price * 2 * self.cfg["paper"]["slippage_pct"] / 100
            lots = min(lots, math.floor(max_loss / (per_unit * lot_size)))
        while lots > 0:
            qty = lots * lot_size
            cost = price * qty + order_charges(price * qty, "BUY", self.cfg["charges"])
            if cost <= budget:
                return qty
            lots -= 1
        return 0

    def slip(self, price: float, side: str) -> float:
        s = self.cfg["paper"]["slippage_pct"] / 100
        return tick(price * (1 + s) if side == "BUY" else price * (1 - s))

    # ---------------- decisions ----------------
    def can_enter(self, now: datetime) -> bool:
        t = now.astimezone(IST).time()
        return (self.position is None
                and self.trades_today < self.cfg["trade"]["max_trades_per_day"]
                and self._t("no_entry_before") <= t <= self._t("last_entry"))

    def entry_signal(self, now: datetime, index_closed: list[dict]) -> Setup | None:
        """Called once per finished 1-min candle with Nifty's finished 1-min candles."""
        if not self.can_enter(now):
            return None
        setup, note = find_setup(self.cfg, index_closed, now, self._seen_bars)
        if note:
            self.note(note)
        return setup

    def index_exit(self, bar: dict) -> str | None:
        """Nifty-based exit on the latest finished 1-min Nifty candle (stop, breakeven stop, target)."""
        p = self.position
        if not p:
            return None
        was_moved = p.spot_stop_moved
        why = spot_exit(self.cfg, p, bar)
        if p.spot_stop_moved and not was_moved:
            self.log(f"Nifty reached {p.spot_entry + (1 if p.contract.opt_type == 'CE' else -1) * p.risk_pts:.0f}: "
                     f"stop moved to the entry level {p.spot_stop:.0f}. This trade can no longer lose much.")
        return why

    # ---------------- fills ----------------
    def open(self, contract: Contract, market_price: float, now: datetime, setup: Setup,
             filled_qty: int | None = None) -> Position | None:
        """Paper: fill at last price plus slippage. Live: pass the real fill price and filled_qty."""
        live = filled_qty is not None
        price = market_price if live else self.slip(market_price, "BUY")
        qty = filled_qty if live else self.size(price, contract.lot_size, setup.risk_pts)
        self.trades_today += 1  # the day's one signal is used up, whatever happens; don't chase
        if qty == 0:
            self.log(f"{now:%H:%M} Signal on {contract.symbol} but even 1 lot at ₹{price} (x{contract.lot_size}) "
                     f"is over ₹{self.cfg['capital']['max_invested']:,} or risks more than "
                     f"₹{self.cfg['trade'].get('max_loss_per_trade', 0):,}. No trade today.")
            return None
        tr = self.cfg["trade"]
        delta = self.cfg["orb"].get("delta_guess", 0.6)
        stop = tick(price * (1 - tr["stop_loss_pct"] / 100))
        self.position = Position(
            contract=contract, qty=qty, entry_price=price, entry_time=now,
            target=tick(price + abs(setup.target - setup.spot) * delta), stop=stop,
            entry_charges=order_charges(price * qty, "BUY", self.cfg["charges"]), reason=setup.reason,
            peak=price, initial_stop=stop, spot_entry=setup.spot, spot_stop=setup.stop,
            spot_target=setup.target, risk_pts=setup.risk_pts)
        p = self.position
        self.log(f"{now:%H:%M:%S} {'LIVE' if live else 'PAPER'} BUY {qty} x {contract.symbol} @ ₹{price} "
                 f"(₹{price * qty:,.0f}). Exit if Nifty closes {'below' if contract.opt_type == 'CE' else 'above'} "
                 f"{p.spot_stop:.0f}, book profit at Nifty {p.spot_target:.0f}, safety stop ₹{p.stop}. "
                 f"Why: {setup.reason}")
        return p

    def check_price(self, ltp: float) -> str | None:
        """Option safety stop: exit if the option price itself has fallen to the stop."""
        p = self.position
        if p and ltp <= p.stop:
            return "OPTION_STOP"
        if p:
            p.peak = max(p.peak, ltp)
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
