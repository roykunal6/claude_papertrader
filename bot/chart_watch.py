"""Act on the REAL TrendIkaterr indicator on your INDstocks chart. Nothing is re-built here.

The Chrome extension in chart_watcher/ reads the indicator's colour straight off the open
INDstocks chart every 2 seconds (blue = bullish, red = bearish, green = recovery) and sends it to
this program on your own Mac (http://127.0.0.1:8765). This program turns colour changes into
alerts and paper trades. It never places real orders.

Colour changes are judged on CLOSED 1-minute candles (like TradingView's "once per bar close"),
so a colour that flickers inside a running candle doesn't fire.
"""
from __future__ import annotations

import json
import re
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .alerts import Alerter, contract_label
from .broker import tick
from .costs import order_charges
from .instruments import Contract, nearest_expiry, pick, pick_strike
from .strategy import IST, Trade, hhmm

STATE_WORDS = {"BULLISH": "BLUE (bullish)", "BEARISH": "RED (bearish)", "RECOVERY": "GREEN (recovery)"}


# ---------------------------------------------------------------- readings from the extension
class Readings:
    """Latest reading per chart pane, written by the web server thread, read by the main loop."""

    def __init__(self, clock=_time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self.panes: dict[str, dict] = {}
        self.last_contact = None   # any message from the extension (even while you hover the chart)
        self.hovering = False
        self.status_text = "Bot starting"

    def update(self, msg: dict) -> None:
        with self._lock:
            now = self._clock()
            self.last_contact = now
            self.hovering = bool(msg.get("hovering"))
            for p in msg.get("panes", []):
                name = str(p.get("pane", "")).strip()
                if name:
                    self.panes[name] = {"state": p.get("state"), "value": p.get("value"),
                                        "colour": p.get("colour"), "at": now}

    def pane_names(self) -> list[str]:
        with self._lock:
            return list(self.panes)

    def state(self, pane: str, max_age: float = 15.0) -> str | None:
        """The pane's state if it was read in the last few seconds, else None."""
        with self._lock:
            r = self.panes.get(pane)
            if not r or self._clock() - r["at"] > max_age:
                return None
            return r["state"]

    def seconds_since_contact(self) -> float | None:
        with self._lock:
            return None if self.last_contact is None else self._clock() - self.last_contact


def start_server(readings: Readings, port: int, log=print) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            # lets a script on the INDstocks page itself talk to this Mac-only server (no extension needed)
            self.send_header("Access-Control-Allow-Origin", "https://www.indstocks.com")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            if self.path != "/signal":
                return self._reply(404, {"ok": False})
            try:
                n = int(self.headers.get("Content-Length", 0))
                readings.update(json.loads(self.rfile.read(min(n, 100_000)) or b"{}"))
            except (ValueError, json.JSONDecodeError):
                return self._reply(400, {"ok": False})
            self._reply(200, {"ok": True, "text": readings.status_text})

        def do_OPTIONS(self):  # noqa: N802
            self._reply(200, {"ok": True})

        def do_GET(self):  # noqa: N802
            self._reply(200, {"ok": True, "text": readings.status_text})

        def log_message(self, *args):  # keep the terminal quiet
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"Listening for the chart watcher on http://127.0.0.1:{port}")
    return srv


# ---------------------------------------------------------------- closed-candle colour changes
class BarCloseTracker:
    """Feeds the live colour once a second; reports a change only when a 1-minute candle closes
    in a different colour from the previous closed candle."""

    def __init__(self):
        self.minute = None
        self.last = None      # latest colour seen in the running minute
        self.closed = None    # colour of the last closed candle

    def feed(self, state: str | None, now: datetime) -> tuple[str, str] | None:
        m = now.replace(second=0, microsecond=0)
        change = None
        if self.minute is not None and m != self.minute:
            if self.last is not None:
                if self.closed is not None and self.last != self.closed:
                    change = (self.closed, self.last)
                self.closed = self.last
            self.last = None
        self.minute = m
        if state is not None:
            self.last = state
        return change


# ---------------------------------------------------------------- paper trades on those changes
@dataclass
class PaperPosition:
    contract: Contract
    qty: int
    entry_price: float
    entry_time: datetime
    entry_charges: float
    peak: float
    stop: float


class ChartTrader:
    """Paper-trades TrendIkaterr colour changes.

    follow = "options": every Call/Put pane on the chart is its own signal. A candle closing BLUE on a
             pane buys that pane's option; RED on that pane sells it. Several trades a day, CE and PE
             can be held at the same time.
    follow = "nifty":   only the NIFTY 50 pane. Blue buys an in-the-money call; red sells it
             (and with on_red = "buy_pe" also buys a put).
    """

    def __init__(self, cfg: dict, contracts: list[Contract], expiry, quote, alert: Alerter, log=print,
                 record=None):
        self.cfg = cfg
        self.w = cfg["chart_watch"]
        self.contracts, self.expiry = contracts, expiry
        self.quote = quote            # quote(scrip) -> last price or None
        self.alert, self.log = alert, log
        self.record = record or (lambda trade: None)
        self.positions: dict[str, PaperPosition] = {}   # pane name -> open paper position
        self.closed: list[Trade] = []
        self.done_for_day = ""

    @property
    def position(self) -> PaperPosition | None:   # the first open position (handy in nifty mode / tests)
        return next(iter(self.positions.values()), None)

    def follows(self, pane: str) -> bool:
        if self.w.get("follow", "nifty") == "options":
            return pane_contract_key(pane) is not None
        return pane == self.w["pane"]

    # ---- rules
    def day_pnl(self) -> float:
        return sum(t.net_pnl for t in self.closed)

    def invested(self) -> float:
        return sum(p.entry_price * p.qty for p in self.positions.values())

    def can_enter(self, now: datetime) -> str:
        """'' if a new trade is allowed, else the reason why not."""
        w = self.w
        if not w.get("auto_buy", True):
            return "auto-buy is off (alerts only)"
        if self.done_for_day:
            return self.done_for_day
        if now.time() < hhmm(w["no_entry_before"]) or now.time() > hhmm(w["last_entry"]):
            return f"outside buying hours ({w['no_entry_before']} to {w['last_entry']})"
        if len(self.closed) + len(self.positions) >= w["max_trades_per_day"]:
            return f"already {w['max_trades_per_day']} trades today"
        return ""

    def _check_day_limits(self) -> None:
        w, pnl = self.w, self.day_pnl()
        losers = sum(1 for t in self.closed if t.net_pnl < 0)
        if self.done_for_day:
            return
        if w.get("daily_profit_stop") and pnl >= w["daily_profit_stop"]:
            self.done_for_day = f"day profit ₹{pnl:,.0f} reached the ₹{w['daily_profit_stop']:,} goal"
        elif w.get("daily_loss_stop") and pnl <= -w["daily_loss_stop"]:
            self.done_for_day = f"day loss ₹{-pnl:,.0f} hit the ₹{w['daily_loss_stop']:,} limit"
        elif w.get("max_losing_trades") and losers >= w["max_losing_trades"]:
            self.done_for_day = f"{losers} losing trades today"
        if self.done_for_day:
            self.alert.send("🛑 Done for today", f"No more buys today: {self.done_for_day}.", speak="Done for today.")

    def _contract_for(self, pane: str, new: str, spot: float | None) -> Contract | None:
        if self.w.get("follow", "nifty") == "options":
            key = pane_contract_key(pane)
            if not key:
                return None
            day, mon, strike, typ = key
            for c in self.contracts:
                if (c.opt_type == typ and abs(c.strike - strike) < 0.01 and c.expiry.day == day
                        and c.expiry.strftime("%b").lower() == mon.lower()):
                    return c
            self.log(f"Couldn't find the contract for '{pane}' in the instrument list.")
            return None
        want = "CE" if new == "BULLISH" else "PE"
        if not spot:
            self.log("Not buying: couldn't get the Nifty price.")
            return None
        k = pick_strike(spot, want, self.cfg["trade"]["strike_step"], self.cfg["trade"].get("strike_mode", "itm"))
        return pick(self.contracts, self.expiry, k, want)

    # ---- events
    def on_change(self, old: str, new: str, spot: float | None, now: datetime, pane: str | None = None) -> None:
        pane = pane or self.w["pane"]
        options = self.w.get("follow", "nifty") == "options"
        self.alert.send(f"{'🔵' if new == 'BULLISH' else '🔴' if new == 'BEARISH' else '🟢'} TrendIkaterr turned "
                        f"{STATE_WORDS.get(new, new)}",
                        f"{pane} on your chart: {STATE_WORDS.get(old, old)} → {STATE_WORDS.get(new, new)} "
                        f"(1-min candle closed {now - timedelta(minutes=1):%H:%M}).",
                        speak=f"Trend ikaterr {'blue' if new == 'BULLISH' else 'red' if new == 'BEARISH' else 'green'}.")
        p = self.positions.get(pane)
        if p and (new == "BEARISH" if (options or p.contract.opt_type == "CE") else new == "BULLISH"):
            self.exit(now, "TREND_FLIP", pane=pane)
        if options:
            buy = new == "BULLISH"
        else:
            buy = new == "BULLISH" or (new == "BEARISH" and self.w.get("on_red") == "buy_pe")
        if not buy or pane in self.positions:
            return
        why_not = self.can_enter(now)
        if why_not:
            self.log(f"Not buying: {why_not}.")
            return
        c = self._contract_for(pane, new, spot)
        ltp = self.quote(c.scrip) if c else None
        if not ltp:
            self.log(f"Not buying: no price for {pane}.")
            return
        price = tick(ltp * (1 + self.cfg["paper"]["slippage_pct"] / 100))
        cap = self.w.get("max_capital", self.cfg["capital"]["max_invested"])
        room = cap - self.invested()
        lots = self.w.get("lots", 0) or min(int(room // (price * c.lot_size)), self.cfg["trade"]["max_lots"])
        if lots < 1 or price * c.lot_size * lots > room:
            self.alert.send("⚠️ No trade", f"Blue on {contract_label(c)}, but {lots or 1} lot (₹{price * c.lot_size * max(lots, 1):,.0f}) "
                            f"would take the money in use over ₹{cap:,} (₹{self.invested():,.0f} already in trades).")
            return
        qty = lots * c.lot_size
        stop = tick(price * (1 - self.w["stop_pct"] / 100))
        self.positions[pane] = PaperPosition(c, qty, price, now, order_charges(price * qty, "BUY", self.cfg["charges"]),
                                             price, stop)
        self.alert.send("🟢 PAPER BUY", f"{contract_label(c)}: {lots} lot(s), qty {qty}, at ₹{price} "
                        f"(₹{price * qty:,.0f}). Stop ₹{stop}, trailing {self.w['trail_pct']}% behind the best price. "
                        "Sells when TrendIkaterr turns red, at the stop, or at "
                        f"{self.cfg['timing']['square_off']}. Not a real order.",
                        speak=f"Paper buy. Nifty {c.strike:.0f} {'call' if c.opt_type == 'CE' else 'put'}.")

    def on_price(self, ltp: float | None, now: datetime, pane: str | None = None) -> None:
        pane = pane or next(iter(self.positions), None)
        p = self.positions.get(pane) if pane else None
        if not p or not ltp:
            return
        if ltp > p.peak:
            p.peak = ltp
            p.stop = max(p.stop, tick(p.peak * (1 - self.w["trail_pct"] / 100)))
        if now.time() >= hhmm(self.cfg["timing"]["square_off"]):
            self.exit(now, "TIME_3:15", ltp, pane=pane)
        elif ltp <= p.stop:
            self.exit(now, "TRAIL_STOP" if p.stop > p.entry_price else "OPTION_STOP", ltp, pane=pane)

    def exit(self, now: datetime, reason: str, ltp: float | None = None, pane: str | None = None) -> Trade | None:
        pane = pane or next(iter(self.positions), None)
        p = self.positions.pop(pane, None) if pane else None
        if not p:
            return None
        ltp = ltp or self.quote(p.contract.scrip) or p.entry_price
        price = tick(ltp * (1 - self.cfg["paper"]["slippage_pct"] / 100))
        charges = round(p.entry_charges + order_charges(price * p.qty, "SELL", self.cfg["charges"]), 2)
        gross = round((price - p.entry_price) * p.qty, 2)
        t = Trade(date=f"{p.entry_time:%Y-%m-%d}", symbol=p.contract.symbol, opt_type=p.contract.opt_type,
                  strike=p.contract.strike, qty=p.qty, entry_time=f"{p.entry_time:%H:%M:%S}",
                  entry_price=p.entry_price, exit_time=f"{now:%H:%M:%S}", exit_price=price, exit_reason=reason,
                  gross_pnl=gross, charges=charges, net_pnl=round(gross - charges, 2),
                  entry_reason="TrendIkaterr turned blue on the INDstocks chart")
        self.closed.append(t)
        self.record(t)
        why = {"TREND_FLIP": "TrendIkaterr changed colour", "TRAIL_STOP": "trailing stop hit, profit locked",
               "OPTION_STOP": "stop hit", "TIME_3:15": "3:15 PM square-off",
               "MANUAL_STOP": "you asked it to stop"}.get(reason, reason)
        self.alert.send("🔴 PAPER SELL", f"{t.symbol} qty {t.qty} at ₹{price} ({why}). "
                        f"P&L ₹{t.net_pnl:,.0f} after charges. Day so far ₹{self.day_pnl():,.0f}.",
                        speak="Paper sell.")
        self._check_day_limits()
        return t

    def exit_all(self, now: datetime, reason: str) -> None:
        for pane in list(self.positions):
            self.exit(now, reason, pane=pane)


_PANE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+₹?\s*([\d,]+(?:\.\d+)?)\s+(Call|Put)\b", re.I)


def pane_contract_key(pane: str):
    """'NIFTY 01 Dec ₹25850 Call' -> (1, 'Dec', 25850.0, 'CE'); None for panes that aren't an option."""
    m = _PANE_RE.search(pane or "")
    if not m:
        return None
    return int(m.group(1)), m.group(2), float(m.group(3).replace(",", "")), "CE" if m.group(4).lower() == "call" else "PE"


# ---------------------------------------------------------------- the day's loop
def run_watch(cfg: dict) -> None:
    from pathlib import Path

    from .indstocks import ApiError, IndstocksClient
    from .live import Journal, append_trade
    from .market import load_contracts

    w = cfg["chart_watch"]
    options = w.get("follow", "nifty") == "options"
    today = datetime.now(IST).date()
    log = Journal(today)
    log("=" * 70)
    log(f"TrendIkaterr chart watcher for {today}. PAPER only: no real orders are placed.")
    readings = Readings()
    readings.status_text = "Bot connected (starting up)"
    start_server(readings, int(w.get("port", 8765)), log)

    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    client.profile()
    contracts = load_contracts(client, cfg, log)
    expiry = nearest_expiry(contracts, today)
    if expiry == today and cfg["trade"].get("expiry_day_contract", "next") == "next":
        expiry = nearest_expiry(contracts, today + timedelta(days=1))
    if not contracts or not expiry:
        log("Couldn't load Nifty option contracts. Stopping.")
        return
    log(f"Loaded {len(contracts)} Nifty option contracts.")

    alert = Alerter(cfg, log)

    def quote(scrip: str) -> float | None:
        try:
            return client.ltp([scrip]).get(scrip)
        except ApiError as e:
            log(f"Price problem: {e}")
            return None

    trader = ChartTrader(cfg, contracts, expiry, quote, alert, log, record=lambda t: append_trade(t, "paper-chart"))
    trackers: dict[str, BarCloseTracker] = {}
    idx = cfg["api"]["index_scrip"]
    square_off = hhmm(cfg["timing"]["square_off"])
    stop_file = Path(__file__).resolve().parent.parent / "STOP"
    what = ("every Call and Put chart: blue = paper buy that option (1 lot), red = sell it" if options else
            f"the {w['pane']} chart: blue = paper buy a call, red = sell")
    alert.send("✅ Watching the real TrendIkaterr", f"Keep the INDstocks chart open and visible in Chrome. I follow {what}.")
    last_warn, last_states, said_seen = 0.0, {}, False
    open_t, close_t = hhmm("09:15"), (datetime.combine(today, square_off) + timedelta(minutes=2)).time()

    while True:
        now = datetime.now(IST)
        if now.time() >= close_t:
            break
        if stop_file.exists():
            log("STOP file found: closing any paper positions and stopping.")
            trader.exit_all(now, "MANUAL_STOP")
            break
        panes = [n for n in readings.pane_names() if trader.follows(n)]
        states = {n: readings.state(n) for n in panes}
        seen = {n: s for n, s in states.items() if s}
        readings.status_text = ("Bot connected · paper · " + (" · ".join(f"{n}: {STATE_WORDS.get(s, s)}" for n, s in seen.items())
                                                             or "TrendIkaterr not seen yet")
                                + (f" · holding {', '.join(contract_label(p.contract) for p in trader.positions.values())}"
                                   if trader.positions else "")
                                + (f" · day P&L ₹{trader.day_pnl():,.0f}" if trader.closed else ""))
        if seen and not said_seen:
            said_seen = True
            alert.send("👀 Chart connected", "I can see TrendIkaterr on: "
                       + ", ".join(f"{n} {STATE_WORDS.get(s, s)}" for n, s in seen.items()) + ".")
        for n, s in seen.items():
            if s != last_states.get(n):
                log(f"{n} live colour now {STATE_WORDS.get(s, s)} (acts when the candle closes).")
                last_states[n] = s
        # ---- warn if the chart stopped talking during market hours
        gap = readings.seconds_since_contact()
        silent = gap is None or gap > 60 or (not seen and not readings.hovering)
        if open_t <= now.time() < square_off and silent and _time.time() - last_warn > 300:
            last_warn = _time.time()
            alert.send("⚠️ Can't see the chart",
                       "Open indstocks.com/app/flash in Chrome with TRENDIKATERR on the charts, keep the window "
                       "visible (not minimised) and move the mouse off the chart.", speak="Check the chart window.")
        # ---- act on closed candles, pane by pane
        for n in set(panes) | set(trackers):
            change = trackers.setdefault(n, BarCloseTracker()).feed(states.get(n), now)
            if change:
                spot = quote(idx) if not options and change[1] in ("BULLISH", "BEARISH") else None
                trader.on_change(change[0], change[1], spot, now, pane=n)
        for n, p in list(trader.positions.items()):
            trader.on_price(quote(p.contract.scrip), now, pane=n)
        _time.sleep(1)

    trader.exit_all(datetime.now(IST), "TIME_3:15")
    n = len(trader.closed)
    wins = sum(1 for t in trader.closed if t.net_pnl > 0)
    alert.send("📊 Day summary", f"{n} paper trade(s) on TrendIkaterr today ({wins} won), P&L ₹{trader.day_pnl():,.0f} "
               "after charges." if n else "No paper trades today.")
