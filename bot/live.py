"""Run the strategy on today's live market. Paper by default; real orders only via `run.py live`."""
from __future__ import annotations

import csv
import json
import time as _time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .indstocks import ApiError, IndstocksClient
from .instruments import Contract, nearest_expiry, pick, pick_strike
from .alerts import Alerter, contract_label
from .broker import LiveBroker, tick
from .market import load_contracts
from .orb import is_five_min_close
from .replay import closed_upto
from .strategy import IST, Position, Strategy, Trade, hhmm

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
TRADES_CSV = ROOT / "trades.csv"


class Journal:
    def __init__(self, day: date):
        LOGS.mkdir(exist_ok=True)
        self.path = LOGS / f"{day:%Y-%m-%d}.log"

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now(IST):%H:%M:%S}] {msg}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def append_trade(trade: Trade, mode: str) -> None:
    new = not TRADES_CSV.exists()
    row = {"mode": mode, **asdict(trade)}
    with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


class StateFile:
    """Remembers today's trade count and open position so a restart can't take a second trade."""
    def __init__(self, day: date):
        self.path = ROOT / "state.json"
        self.day = day.isoformat()
        self.locked: dict = {}

    def load(self, strat: Strategy) -> None:
        if not self.path.exists():
            return
        s = json.loads(self.path.read_text())
        if s.get("day") != self.day:
            return
        strat.trades_today = s.get("trades_today", 0)
        self.locked = s.get("locked", {})
        if s.get("position"):
            p = s["position"]
            c = p["contract"]
            c["expiry"] = date.fromisoformat(c["expiry"])
            p["contract"] = Contract(**c)
            p["entry_time"] = datetime.fromisoformat(p["entry_time"])
            strat.position = Position(**p)

    def save(self, strat: Strategy) -> None:
        pos = None
        if strat.position:
            pos = asdict(strat.position)
            pos["contract"]["expiry"] = strat.position.contract.expiry.isoformat()
            pos["entry_time"] = strat.position.entry_time.isoformat()
        self.path.write_text(json.dumps({"day": self.day, "trades_today": strat.trades_today,
                                         "position": pos, "locked": self.locked}, indent=2))


class CandleCache:
    def __init__(self, client: IndstocksClient):
        self.client = client
        self.data: dict[str, list[dict]] = {}

    def refresh(self, scrips: list[str], now: datetime) -> None:
        cold = [s for s in scrips if s not in self.data]
        if cold:  # first time: pull ~5 days so the trend line is warmed up
            start = now - timedelta(days=5)
            got = self.client.candles(cold, int(start.timestamp() * 1000), int(now.timestamp() * 1000))
            for s in cold:
                self.data[s] = got.get(s, [])
        warm = [s for s in scrips if s not in cold]
        if warm:
            start = now - timedelta(minutes=15)
            got = self.client.candles(warm, int(start.timestamp() * 1000), int(now.timestamp() * 1000))
            for s in warm:
                merged = {c["ts"]: c for c in self.data[s]}
                merged.update({c["ts"]: c for c in got.get(s, [])})
                self.data[s] = [merged[k] for k in sorted(merged)]


def _buy_alert(alert: Alerter, p, live: bool = False) -> None:
    lots = p.qty // p.contract.lot_size
    alert.send("🟢 BOUGHT (real order)" if live else "🟢 BUY NOW",
               f"{contract_label(p.contract)}: {lots} lot(s), qty {p.qty}, around ₹{p.entry_price}. "
               f"I'll tell you when to sell: if Nifty closes {'below' if p.contract.opt_type == 'CE' else 'above'} "
               f"{p.spot_stop:.0f} (stop, it moves up as the trade goes your way), "
               + (f"or reaches {p.spot_target:.0f} (profit), " if p.spot_target else "")
               + f"or the option falls to ₹{p.stop}.",
               speak=("Bought. " if live else "Buy now. ") + f"Nifty {p.contract.strike:.0f} {'call' if p.contract.opt_type == 'CE' else 'put'}.")


def _sell_alert(alert: Alerter, trade, live: bool = False) -> None:
    why = {"TARGET": "Nifty reached the profit target", "STOP_LOSS": "stop hit",
           "TRAIL_STOP": "trailing stop hit, profit locked", "TREND_END": "the trend has ended",
           "BREAKEVEN_STOP": "Nifty came back to the entry level", "OPTION_STOP": "option safety stop hit",
           "TIME_3:15": "3:15 PM square-off", "MANUAL_STOP": "you asked it to stop"}.get(trade.exit_reason, trade.exit_reason)
    if live:
        alert.send("🔴 SOLD (real order)",
                   f"Sold all {trade.qty} qty {trade.symbol} ({why}) at ₹{trade.exit_price}. "
                   f"P&L about ₹{trade.net_pnl:,.0f} after charges.", speak="Sold.")
        return
    alert.send("🔴 SELL NOW",
               f"Sell ALL {trade.qty} qty {trade.symbol} ({why}), around ₹{trade.exit_price}. "
               f"P&L ₹{trade.net_pnl:,.0f} after charges.",
               speak="Sell now.")


def run_live(cfg: dict, live: bool = False) -> None:
    today = datetime.now(IST).date()
    log = Journal(today)
    log("=" * 70)
    mode = "live" if live else "paper"
    log(f"Nifty options bot starting for {today} in {'LIVE (REAL MONEY)' if live else 'PAPER'} mode."
        + ("" if live else " No real orders are placed."))
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    client.profile()
    log("API token OK.")

    contracts = load_contracts(client, cfg, log)
    expiry = nearest_expiry(contracts, today)
    if not contracts or not expiry:
        log("Couldn't find Nifty option contracts in the instrument list. Stopping.")
        return
    log(f"Loaded {len(contracts)} Nifty option contracts. Nearest expiry {expiry}.")
    if expiry == today:
        if cfg["trade"]["skip_expiry_day"]:
            log("Today is a Nifty expiry day. No trading today (your rule).")
            return
        if cfg["trade"].get("expiry_day_contract", "next") == "next":
            expiry = nearest_expiry(contracts, today + timedelta(days=1))
            log(f"Expiry day: trading next week's contract (expiry {expiry}) instead of today's.")
    if today.weekday() >= 5:
        log("Weekend. Market closed.")
        return

    alert = Alerter(cfg, log)
    strat = Strategy(cfg, log=log)
    state = StateFile(today)
    state.load(strat)
    if strat.trades_today:
        log(f"Restarted: {strat.trades_today} trade(s) already taken today.")
    broker = LiveBroker(client, cfg, log) if live else None
    max_trades = cfg["trade"]["max_trades_per_day"]
    if broker:
        # Never trust only the local file with real money: check the account too.
        held = broker.open_nifty_positions()
        if held and not strat.position:
            alert.send("⚠️ Open Nifty position found",
                       f"Your account already holds {held[0].get('symbol')} (qty {held[0].get('net_qty')}) "
                       "that this bot doesn't know about. The bot will NOT trade today. Manage it in the app.")
            strat.trades_today = max_trades
        if strat.trades_today < max_trades and any(str(o.get("txn_type")).upper() == "BUY"
                                                    for o in broker.bot_orders_today()):
            log("The order book shows the bot already bought today. No more entries today.")
            strat.trades_today = max_trades
        funds = broker.option_buy_funds()
        log(f"Funds available for option buying: ₹{funds:,.0f}" if funds is not None else
            "Couldn't read funds; will check again before buying.")
    last_nag = 0.0

    def do_exit(ltp: float, now: datetime, reason: str) -> None:
        """Close the position: paper fill, or a real sell order in live mode."""
        nonlocal last_nag
        p = strat.position
        if not broker:
            t = strat.close(ltp, now, reason)
            append_trade(t, mode)
            _sell_alert(alert, t)
            state.save(strat)
            return
        fill = broker.sell(p.contract, p.qty, ltp, get_ltp=lambda: client.ltp([p.contract.scrip]).get(p.contract.scrip))
        if fill.qty >= p.qty:
            t = strat.close(fill.price, now, reason, exact=True, live=True)
            append_trade(t, mode)
            _sell_alert(alert, t, live=True)
        else:
            p.qty -= fill.qty
            p.pending_exit = reason  # keep trying every second
            if _time.time() - last_nag > 30:
                last_nag = _time.time()
                alert.send("🚨 SELL BY HAND NOW",
                           f"The bot could not sell all of {p.contract.symbol}. {p.qty} qty still open. "
                           f"Open the INDmoney app and sell it. ({fill.note})", speak="Warning. Sell by hand now.")
        state.save(strat)

    cache = CandleCache(client)
    idx = cfg["api"]["index_scrip"]
    step = cfg["trade"]["strike_step"]
    square_off = hhmm(cfg["timing"]["square_off"])
    last_minute_done = None
    strike_mode = cfg["trade"].get("strike_mode", "itm")
    range_end = hhmm(cfg["orb"]["range_end"])
    range_told = False
    stop_file = ROOT / "STOP"
    alert.send("✅ Bot is running", f"{'LIVE trading (real money)' if live else 'Paper trading'} Nifty options today ({today}). "
               + (f"I'll watch Nifty from 9:15 to {cfg['orb']['range_end']}, then tell you exactly when to buy and sell."
                if strat.name == "orb" else "I'll only trade if Nifty shows a clear trend, and tell you exactly when to buy and sell."))

    def buy_live(contract, ltp: float, now: datetime, setup) -> None:
        strat.trades_today += 1  # the day's one signal is used, whatever happens next
        limit = tick(ltp * (1 + broker.buffer))
        qty = strat.size(limit, contract.lot_size, setup.risk_pts)
        if qty == 0:
            log(f"1 lot of {contract.symbol} at ₹{limit} is more than ₹{cfg['capital']['max_invested']}. No trade.")
            return
        funds = broker.option_buy_funds()
        if funds is not None and funds < limit * qty + 50:
            alert.send("⚠️ Not enough funds", f"Needed about ₹{limit * qty:,.0f}, account has ₹{funds:,.0f}. No trade today.")
            return
        fill = broker.buy(contract, qty, ltp)
        if fill.qty <= 0:
            alert.send("⚠️ Buy did not go through", f"{contract.symbol}: {fill.note} Nothing was bought. No more trades today.")
            return
        strat.trades_today -= 1  # open() counts the trade itself
        strat.open(contract, fill.price, now, setup, filled_qty=fill.qty)
        if fill.note:
            log(fill.note)
        _buy_alert(alert, strat.position, live=True)

    while True:
        now = datetime.now(IST)
        if now.time() >= (datetime.combine(today, square_off) + timedelta(minutes=2)).time():
            break
        try:
            # ---- every second: watch target/stop on the open position ----
            if stop_file.exists():
                log("STOP file found: closing any position and stopping.")
                if strat.position:
                    p = strat.position
                    do_exit(client.ltp([p.contract.scrip]).get(p.contract.scrip) or p.entry_price, now, "MANUAL_STOP")
                if not strat.position:
                    break
            if strat.position:
                p = strat.position
                ltp = client.ltp([p.contract.scrip]).get(p.contract.scrip)
                if ltp is not None:
                    reason = getattr(p, "pending_exit", None) or strat.check_price(ltp)
                    if reason:
                        do_exit(ltp, now, reason)
                if strat.position and now.time() >= square_off:
                    do_exit(ltp or p.entry_price, now, "TIME_3:15")

            # ---- once per minute, 3 seconds after the candle closes ----
            minute = now.replace(second=0, microsecond=0)
            if now.second >= 3 and minute != last_minute_done and now.time() >= hhmm("09:16"):
                last_minute_done = minute
                close_ts = int(minute.timestamp())
                if strat.position or strat.can_enter(now) or not range_told:
                    cache.refresh([idx], now)
                index_c = closed_upto(cache.data.get(idx, []), close_ts)
                today_c = [c for c in index_c if datetime.fromtimestamp(c["ts"], IST).date() == today]
                if not range_told and strat.name == "orb" and now.time() >= range_end and today_c:
                    range_told = True
                    from .orb import opening_range, range_problem
                    rng = opening_range(today_c, range_end)
                    if rng:
                        problem = range_problem(rng, cfg["orb"])
                        alert.send("📏 Opening range set",
                                   f"Nifty moved between {rng.low:.0f} and {rng.high:.0f} since 9:15. "
                                   + (problem if problem else
                                      f"I'll buy a CALL if Nifty breaks above {rng.high:.0f}, "
                                      f"a PUT if it breaks below {rng.low:.0f}. Nothing to do until then."))
                if strat.position and today_c:
                    before = strat.position.spot_stop
                    why = strat.index_exit(today_c[-1], index_c, minute)
                    state.save(strat)
                    if not why and strat.position.spot_stop != before:
                        alert.send("⬆️ Stop moved", f"{strat.position.contract.symbol}: now exits if Nifty closes "
                                   f"{'below' if strat.position.contract.opt_type == 'CE' else 'above'} "
                                   f"{strat.position.spot_stop:.0f}. Nothing to do.")
                    if why:
                        s = strat.position.contract.scrip
                        do_exit(client.ltp([s]).get(s) or strat.position.entry_price, now, why)
                elif strat.can_enter(now):
                    if not today_c:
                        log("No Nifty index candles yet.")
                    else:
                        setup = strat.entry_signal(now, index_c) if is_five_min_close(minute) else None
                        if setup:
                            k = pick_strike(setup.spot, setup.opt_type, step, strike_mode)
                            contract = pick(contracts, expiry, k, setup.opt_type)
                            ltp = client.ltp([contract.scrip]).get(contract.scrip) if contract else None
                            if not ltp:
                                strat.trades_today += 1
                                log(f"Signal for {k:.0f} {setup.opt_type} but couldn't get its price. No trade today.")
                            elif not broker:
                                if strat.open(contract, ltp, now, setup):
                                    _buy_alert(alert, strat.position)
                            else:
                                buy_live(contract, ltp, now, setup)
                            state.save(strat)
                        elif minute.minute % 15 == 0:
                            log(f"Watching. Nifty {today_c[-1]['c']:.2f}. No breakout yet.")
        except ApiError as e:
            log(f"API problem: {e}. Retrying.")
            _time.sleep(5)
        _time.sleep(1)

    for _ in range(5):  # safety net: never leave the day with a position open
        if not strat.position:
            break
        p = strat.position
        ltp = client.ltp([p.contract.scrip]).get(p.contract.scrip) or p.entry_price
        do_exit(ltp, datetime.now(IST), getattr(p, "pending_exit", None) or "TIME_3:15")
    total = sum(t.net_pnl for t in strat.closed)
    if strat.trades_today == 0:
        alert.send("😴 No trade today", "No clean signal today, so no trade. That's the plan working: no trade is better than a bad trade.")
    else:
        alert.send("🏁 Day done", f"Trades: {len(strat.closed)}. Net P&L ₹{total:,.0f} after charges.")
    log(f"Log saved: {log.path}")
