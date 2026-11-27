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
    """Paper-trades the indicator's colour changes on one pane (normally NIFTY 50)."""

    def __init__(self, cfg: dict, contracts: list[Contract], expiry, quote, alert: Alerter, log=print,
                 record=None):
        self.cfg = cfg
        self.w = cfg["chart_watch"]
        self.contracts, self.expiry = contracts, expiry
        self.quote = quote            # quote(scrip) -> last price or None
        self.alert, self.log = alert, log
        self.record = record or (lambda trade: None)
        self.position: PaperPosition | None = None
        self.closed: list[Trade] = []
        self.done_for_day = ""

    # ---- rules
    def day_pnl(self) -> float:
        return sum(t.net_pnl for t in self.closed)

    def can_enter(self, now: datetime) -> str:
        """'' if a new trade is allowed, else the reason why not."""
        w = self.w
        if not w.get("auto_buy", True):
            return "auto-buy is off (alerts only)"
        if self.done_for_day:
            return self.done_for_day
        if now.time() < hhmm(w["no_entry_before"]) or now.time() > hhmm(w["last_entry"]):
            return f"outside buying hours ({w['no_entry_before']} to {w['last_entry']})"
        if len(self.closed) + (1 if self.position else 0) >= w["max_trades_per_day"]:
            return f"already {w['max_trades_per_day']} trades today"
        return ""

    def _check_day_limits(self) -> None:
        w, pnl = self.w, self.day_pnl()
        losers = sum(1 for t in self.closed if t.net_pnl < 0)
        if pnl >= w["daily_profit_stop"]:
            self.done_for_day = f"day profit ₹{pnl:,.0f} reached the ₹{w['daily_profit_stop']:,} goal"
        elif pnl <= -w["daily_loss_stop"]:
            self.done_for_day = f"day loss ₹{-pnl:,.0f} hit the ₹{w['daily_loss_stop']:,} limit"
        elif losers >= w["max_losing_trades"]:
            self.done_for_day = f"{losers} losing trades today"
        if self.done_for_day:
            self.alert.send("🛑 Done for today", f"No more buys today: {self.done_for_day}.", speak="Done for today.")

    # ---- events
    def on_change(self, old: str, new: str, spot: float | None, now: datetime) -> None:
        pane = self.w["pane"]
        self.alert.send(f"{'🔵' if new == 'BULLISH' else '🔴' if new == 'BEARISH' else '🟢'} TrendIkaterr turned "
                        f"{STATE_WORDS.get(new, new)}",
                        f"{pane} on your chart: {STATE_WORDS.get(old, old)} → {STATE_WORDS.get(new, new)} "
                        f"(1-min candle closed {now - timedelta(minutes=1):%H:%M}).",
                        speak=f"Trend ikaterr {'blue' if new == 'BULLISH' else 'red' if new == 'BEARISH' else 'green'}.")
        p = self.position
        if p and ((p.contract.opt_type == "CE" and new == "BEARISH") or (p.contract.opt_type == "PE" and new == "BULLISH")):
            self.exit(now, "TREND_FLIP")
        want = "CE" if new == "BULLISH" else "PE" if (new == "BEARISH" and self.w.get("on_red") == "buy_pe") else None
        if not want or self.position:
            return
        why_not = self.can_enter(now)
        if why_not:
            self.log(f"Not buying: {why_not}.")
            return
        if not spot:
            self.log("Not buying: couldn't get the Nifty price.")
            return
        k = pick_strike(spot, want, self.cfg["trade"]["strike_step"], self.cfg["trade"].get("strike_mode", "itm"))
        c = pick(self.contracts, self.expiry, k, want)
        ltp = self.quote(c.scrip) if c else None
        if not ltp:
            self.log(f"Not buying: no price for NIFTY {k:.0f} {want}.")
            return
        price = tick(ltp * (1 + self.cfg["paper"]["slippage_pct"] / 100))
        lots = min(int(self.cfg["capital"]["max_invested"] // (price * c.lot_size)), self.cfg["trade"]["max_lots"])
        if lots < 1:
            self.alert.send("⚠️ No trade", f"1 lot of {contract_label(c)} (₹{price * c.lot_size:,.0f}) is more than "
                            f"₹{self.cfg['capital']['max_invested']:,}.")
            return
        qty = lots * c.lot_size
        stop = tick(price * (1 - self.w["stop_pct"] / 100))
        self.position = PaperPosition(c, qty, price, now, order_charges(price * qty, "BUY", self.cfg["charges"]),
                                      price, stop)
        self.alert.send("🟢 PAPER BUY", f"{contract_label(c)}: {lots} lot(s), qty {qty}, at ₹{price} "
                        f"(₹{price * qty:,.0f}). Stop ₹{stop}, trailing {self.w['trail_pct']}% behind the best price. "
                        "Sells when TrendIkaterr turns red, at the stop, or at "
                        f"{self.cfg['timing']['square_off']}. Not a real order.",
                        speak=f"Paper buy. Nifty {c.strike:.0f} {'call' if want == 'CE' else 'put'}.")

    def on_price(self, ltp: float, now: datetime) -> None:
        p = self.position
        if not p or not ltp:
            return
        if ltp > p.peak:
            p.peak = ltp
            p.stop = max(p.stop, tick(p.peak * (1 - self.w["trail_pct"] / 100)))
        if now.time() >= hhmm(self.cfg["timing"]["square_off"]):
            self.exit(now, "TIME_3:15", ltp)
        elif ltp <= p.stop:
            self.exit(now, "TRAIL_STOP" if p.stop > p.entry_price else "OPTION_STOP", ltp)

    def exit(self, now: datetime, reason: str, ltp: float | None = None) -> Trade | None:
        p = self.position
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
                  entry_reason="TrendIkaterr colour change on the INDstocks chart")
        self.position = None
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


# ---------------------------------------------------------------- the day's loop
def run_watch(cfg: dict) -> None:
    from pathlib import Path

    from .indstocks import ApiError, IndstocksClient
    from .live import Journal, append_trade
    from .market import load_contracts

    w = cfg["chart_watch"]
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
    log(f"Loaded {len(contracts)} Nifty option contracts; buying expiry {expiry}.")

    alert = Alerter(cfg, log)

    def quote(scrip: str) -> float | None:
        try:
            return client.ltp([scrip]).get(scrip)
        except ApiError as e:
            log(f"Price problem: {e}")
            return None

    trader = ChartTrader(cfg, contracts, expiry, quote, alert, log, record=lambda t: append_trade(t, "paper-chart"))
    tracker = BarCloseTracker()
    pane, idx = w["pane"], cfg["api"]["index_scrip"]
    square_off = hhmm(cfg["timing"]["square_off"])
    stop_file = Path(__file__).resolve().parent.parent / "STOP"
    alert.send("✅ Watching the real TrendIkaterr",
               f"Open the INDstocks chart in Chrome and keep that window visible. I act on the {pane} pane: "
               f"blue = paper buy a call{', red = paper buy a put' if w.get('on_red') == 'buy_pe' else ', red = sell'}.")
    last_warn, last_state, said_seen = 0.0, None, False
    open_t, close_t = hhmm("09:15"), (datetime.combine(today, square_off) + timedelta(minutes=2)).time()

    while True:
        now = datetime.now(IST)
        if now.time() >= close_t:
            break
        if stop_file.exists():
            log("STOP file found: closing any paper position and stopping.")
            trader.exit(now, "MANUAL_STOP")
            break
        state = readings.state(pane)
        # ---- tell the extension what we see, for its on-chart badge
        p = trader.position
        readings.status_text = (f"Bot connected · paper · {pane}: {STATE_WORDS.get(state, state or 'not seen')}"
                                + (f" · holding {contract_label(p.contract)}" if p else "")
                                + (f" · day P&L ₹{trader.day_pnl():,.0f}" if trader.closed else ""))
        if state and not said_seen:
            said_seen = True
            alert.send("👀 Chart connected", f"I can see TrendIkaterr on {pane}: {STATE_WORDS.get(state, state)}.")
        if state and state != last_state:
            log(f"{pane} live colour now {STATE_WORDS.get(state, state)} (acts when the candle closes).")
            last_state = state
        # ---- warn if the chart stopped talking during market hours
        gap = readings.seconds_since_contact()
        silent = gap is None or gap > 60 or (state is None and not readings.hovering)
        if open_t <= now.time() < square_off and silent and _time.time() - last_warn > 300:
            last_warn = _time.time()
            alert.send("⚠️ Can't see the chart",
                       "Open indstocks.com/app/flash in Chrome with TRENDIKATERR on the Nifty 50 chart, keep the window "
                       "visible (not minimised) and move the mouse off the chart.", speak="Check the chart window.")
        # ---- act on closed candles
        change = tracker.feed(state, now)
        if change:
            spot = quote(idx) if change[1] in ("BULLISH", "BEARISH") else None
            trader.on_change(change[0], change[1], spot, now)
        if trader.position:
            trader.on_price(quote(trader.position.contract.scrip), now)
        _time.sleep(1)

    if trader.position:
        trader.exit(datetime.now(IST), "TIME_3:15")
    n = len(trader.closed)
    alert.send("📊 Day summary", f"{n} paper trade(s) on TrendIkaterr today, P&L ₹{trader.day_pnl():,.0f} after charges."
               if n else "No paper trades today.")
