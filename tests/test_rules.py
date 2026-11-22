import math
import sys
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.9 / 3.10 (e.g. macOS's built-in python3)
    import tomli as tomllib
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.costs import order_charges  # noqa: E402
from bot.instruments import itm_strikes, load_nifty_options  # noqa: E402
from bot.replay import simulate_day  # noqa: E402
from bot.strategy import IST, Strategy  # noqa: E402
from tests.fake_market import make_day, orb_path  # noqa: E402


def cfg():
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def test_itm_strikes():
    assert itm_strikes(25227.95, 50) == (25200.0, 25250.0)
    assert itm_strikes(25200.0, 50) == (25150.0, 25250.0)  # exactly at a strike is not ITM


def quiet(c=None):
    return lambda m: None


# ---------------- Opening Range Breakout ----------------
def test_breakout_up_buys_itm_call_and_books_target():
    day, idx, opt, contracts, expiry = make_day(path=orb_path(up=True))
    logs = []
    trades = simulate_day(cfg(), day, idx, opt, contracts, expiry, log=logs.append)
    assert len(trades) == 1, logs
    t = trades[0]
    assert t.opt_type == "CE" and t.entry_time >= "09:45" and t.exit_reason == "TARGET"
    assert t.strike < 25230  # in the money at entry
    assert t.net_pnl > 0


def test_breakout_down_buys_put():
    day, idx, opt, contracts, expiry = make_day(path=orb_path(up=False))
    trades = simulate_day(cfg(), day, idx, opt, contracts, expiry, log=quiet())
    assert len(trades) == 1 and trades[0].opt_type == "PE" and trades[0].exit_reason == "TARGET"


def test_failed_breakout_stops_out_small():
    day, idx, opt, contracts, expiry = make_day(path=orb_path(up=True, fail=True))
    trades = simulate_day(cfg(), day, idx, opt, contracts, expiry, log=quiet())
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason in ("STOP_LOSS", "OPTION_STOP") and t.net_pnl < 0
    assert t.net_pnl > -cfg()["trade"]["max_loss_per_trade"] * 1.3


def test_one_trade_a_day_even_after_a_stop():
    path = orb_path(up=True, fail=True)
    path += [path[-1] - 2 * i for i in range(375 - len(path))]  # later breakdown: still no second trade
    day, idx, opt, contracts, expiry = make_day(path=path[:375])
    assert len(simulate_day(cfg(), day, idx, opt, contracts, expiry, log=quiet())) == 1


def test_flat_day_no_trade():
    flat = [25200.0 + (0.5 if i % 2 else -0.5) for i in range(375)]
    day, idx, opt, contracts, expiry = make_day(path=flat)
    logs = []
    assert simulate_day(cfg(), day, idx, opt, contracts, expiry, log=logs.append) == []
    assert any("Too quiet" in m for m in logs)


def test_wild_open_skipped():
    wild = [25200.0 + (150 if (i // 5) % 2 else -150) for i in range(30)] + [25600.0 + i for i in range(345)]
    day, idx, opt, contracts, expiry = make_day(path=wild)
    logs = []
    assert simulate_day(cfg(), day, idx, opt, contracts, expiry, log=logs.append) == []
    assert any("Too wild" in m for m in logs)


def test_breakout_against_the_average_is_not_taken():
    from bot.orb import find_setup
    day, idx, *_ = make_day(path=orb_path(up=True))
    # yesterday Nifty was 400 points lower: the 20-candle average is still far below today's price,
    # so a break DOWN through the range goes against the trend and must be refused
    prev = [dict(c, ts=c["ts"] - 86400, o=c["o"] - 400, h=c["h"] - 400, l=c["l"] - 400, c=c["c"] - 400)
            for c in make_day(path=orb_path(up=False))[1]]
    down = make_day(path=orb_path(up=False))[1]
    seen, notes = set(), []
    for i in range(30, 120):
        now = datetime.fromtimestamp(down[i]["ts"] + 60, IST)
        setup, note = find_setup(cfg(), prev + down[: i + 1], now, seen)
        assert setup is None
        if note:
            notes.append(note)
    assert any("wrong side" in n for n in notes)


def test_stop_moves_to_entry_after_1r():
    from bot.orb import Setup
    s = Strategy(cfg(), log=quiet())
    day, idx, opt, contracts, expiry = make_day(path=orb_path(up=True))
    now = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    setup = Setup("CE", 25250.0, 25210.0, 25330.0, 40.0, "test")
    p = s.open(contracts[0], 100.0, now, setup)
    bar = {"o": 25255, "h": 25291, "l": 25250, "c": 25285}
    assert s.index_exit(bar) is None and p.spot_stop == 25250.0
    assert s.index_exit({"o": 25260, "h": 25262, "l": 25245, "c": 25249}) == "BREAKEVEN_STOP"
    assert s.index_exit({"o": 25300, "h": 25331, "l": 25290, "c": 25320}) == "TARGET"
    assert s.check_price(p.stop) == "OPTION_STOP"
    assert not s.can_enter(now)  # one trade a day


def test_size_respects_budget_and_max_loss():
    s = Strategy(cfg())
    assert s.size(150.0, 65) == 195                # no risk given: 3 lots = ₹29,250 fit in ₹30k
    assert s.size(500.0, 65) == 0                  # one lot = ₹32,500 > ₹30,000
    # 40 Nifty points of risk x 0.6 delta = ₹24/unit, ₹1,560 a lot (+slippage): 1 lot under ₹2,500
    assert s.size(150.0, 65, risk_pts=40) == 65
    assert s.size(300.0, 65, risk_pts=90) == 0     # 90 x 0.6 = ₹54/unit, ₹3,500+ a lot: over ₹2,500, no trade


def test_five_minute_candles_align_to_open():
    from bot.orb import five_minute
    day, idx, *_ = make_day(path=orb_path(up=True))
    bars = five_minute(idx[:12])          # 09:15..09:26: two full 5-min bars, the third isn't finished
    assert len(bars) == 2
    assert bars[0]["o"] == idx[0]["o"] and bars[0]["c"] == idx[4]["c"]
    assert bars[1]["h"] == max(c["h"] for c in idx[5:10])


def test_charges_reasonable():
    buy = order_charges(30000, "BUY", cfg()["charges"])
    sell = order_charges(30000, "SELL", cfg()["charges"])
    assert 10 < buy < 30 and 35 < sell < 60


def test_expiry_day_skipped_when_switched_on():
    c = cfg()
    c["trade"]["skip_expiry_day"] = True
    day, idx, opt, contracts, expiry = make_day(path=orb_path(up=True))
    logs = []
    assert simulate_day(c, day, idx, opt, contracts, day, log=logs.append) == []
    assert "expiry" in logs[0]

def test_instrument_csv_parsing():
    header = ("EXCH,SEGMENT,SECURITY_ID,INSTRUMENT_NAME,EXPIRY_CODE,TRADING_SYMBOL,LOT_UNITS,CUSTOM_SYMBOL,"
              "EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SERIES,"
              "SYMBOL_NAME,UNDERLYING_SCRIP_NAME,FREEZE_QTY,UPPER_LIMIT,LOWER_LIMIT,ISIN,GENERAL_FACTOR,DELIVERY_UNIT")
    rows = [
        "NSE,D,51011,OPTIDX,0,NIFTY-Nov2026-25200-CE,65,x,2026-11-24,25200,CE,0.05,W,OP,X,NIFTY,NIFTY,1800,0,0,,1,1",
        "NSE,D,51012,OPTIDX,0,BANKNIFTY-Nov2026-52000-CE,30,x,2026-11-24,52000,CE,0.05,W,OP,X,BANKNIFTY,BANKNIFTY,900,0,0,,1,1",
        "NSE,D,51013,FUTIDX,0,NIFTY-Nov2026-FUT,65,x,2026-11-24,0,XX,0.05,M,FUT,X,NIFTY,NIFTY,1800,0,0,,1,1",
    ]
    got = load_nifty_options("\n".join([header, *rows]))
    assert len(got) == 1
    assert got[0].scrip == "NFO_51011" and got[0].lot_size == 65 and got[0].expiry == date(2026, 11, 24)


def test_real_indstocks_csv_rows():
    # rows copied from the real INDstocks instrument list (22 Nov 2026)
    header = ("EXCH,SEGMENT,SECURITY_ID,INSTRUMENT_NAME,EXPIRY_CODE,TRADING_SYMBOL,LOT_UNITS,CUSTOM_SYMBOL,"
              "EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SERIES,"
              "SYMBOL_NAME,UNDERLYING_SCRIP_NAME,FREEZE_QTY,UPPER_LIMIT,LOWER_LIMIT,ISIN,GENERAL_FACTOR,DELIVERY_UNIT")
    rows = [
        "NSE,D,84762,OPTSTK,29 Dec 2026,CROMPTON-Dec2026-162.5-CE,2150,CROMPTON 29 DEC 162.5 CE,12/29/2026 14:00,162.50000,CE,1,M,OPTSTK,,,CROMPT GREA CON ELEC LTD,116251,81.96,41.96,,0,",
        "NSE,D,48030,OPTIDX,01 Dec 2026,NIFTY-Dec2026-27950-CE,65,NIFTY 1 DEC 27950 CE,12/01/2026 14:00,27950.00000,CE,5,W,OPTIDX,,,NIFTY,1801,20.25,0.05,,0,",
        "NSE,D,67889,OPTIDX,29 Dec 2026,NIFTYNXT50-Dec2026-78700-PE,25,NIFTYNXT50 29 DEC 78700 PE,12/29/2026 14:00,78700.00000,PE,5,M,OPTIDX,,,NIFTYNXT50,601,7413.95,3421.85,,0,",
    ]
    got = load_nifty_options("\n".join([header, *rows]))
    assert len(got) == 1
    c = got[0]
    assert c.expiry == date(2026, 12, 1) and c.strike == 27950 and c.lot_size == 65 and c.opt_type == "CE"


# ---------------- one strike for CE and PE ----------------
# ---------------- real-order broker, against a fake account ----------------
class FakeAccount:
    """Pretends to be INDstocks. fill_ratio: share of each order that fills."""
    def __init__(self, fill_ratio=1.0, reject=False):
        self.orders, self.fill_ratio, self.reject = {}, fill_ratio, reject

    def place_order(self, txn_type, security_id, qty, limit_price, remarks):
        from bot.indstocks import ApiError
        if self.reject:
            raise ApiError("/order: HTTP 403: static IP not whitelisted")
        oid = f"DRV-{len(self.orders) + 1}"
        filled = int(qty * self.fill_ratio) // 65 * 65
        self.orders[oid] = {"status": "COMPLETE" if filled == qty else "OPEN", "traded_qty": filled,
                            "traded_price": limit_price, "txn_type": txn_type, "remarks": remarks, "qty": qty,
                            "security_id": security_id}
        return {"order_id": oid, "order_status": "INITIATED"}

    def order(self, oid):
        return self.orders[oid]

    def cancel_order(self, oid):
        if self.orders[oid]["status"] != "COMPLETE":
            self.orders[oid]["status"] = "CANCELLED"
        return {}


def _contract():
    from bot.instruments import Contract
    return Contract("NFO_64489", "NIFTY-Nov2026-25800-CE", 25800.0, "CE", date(2026, 11, 30), 65)


def test_live_buy_fills_with_limit_order_and_right_security_id():
    from bot.broker import LiveBroker
    acct = FakeAccount()
    b = LiveBroker(acct, cfg() | {"live": {"limit_buffer_pct": 1.0}}, log=lambda m: None, sleep=lambda s: None)
    f = b.buy(_contract(), 130, 200.0)
    assert (f.qty, f.price) == (130, 202.0)
    o = acct.orders[f.order_id]
    assert o["security_id"] == "64489" and o["txn_type"] == "BUY" and "nifty-bot" in o["remarks"]


def test_live_buy_rejected_buys_nothing():
    from bot.broker import LiveBroker
    b = LiveBroker(FakeAccount(reject=True), cfg(), log=lambda m: None, sleep=lambda s: None)
    f = b.buy(_contract(), 130, 200.0)
    assert f.qty == 0 and "static IP" in f.note


def test_live_sell_retries_until_all_sold():
    from bot.broker import LiveBroker
    acct = FakeAccount(fill_ratio=0.5)
    b = LiveBroker(acct, cfg(), log=lambda m: None, sleep=lambda s: None)
    f = b.sell(_contract(), 260, 200.0)
    assert f.qty == 260 - 65  # 130, then 65, then 0 of the last 65 (half of 65 rounds down)
    assert len(acct.orders) == 3 and all(o["txn_type"] == "SELL" for o in acct.orders.values())


def test_paper_command_never_creates_a_broker():
    import inspect
    from bot import live
    src = inspect.getsource(live.run_live)
    assert "LiveBroker(client, cfg, log) if live else None" in src


# ---------------- backtest on an expired weekly ----------------
def test_backtest_uses_expired_contract_api_for_old_weeks():
    import run as R
    day, idx, opt, contracts, expiry = make_day(path=orb_path(up=True))   # 2026-11-19, weekly expiring later
    old = {c.scrip: c for c in contracts}
    sym = {c.scrip: f"NIFTY{c.strike:.0f}{c.opt_type}" for c in contracts}

    class Client:
        calls = []
        def candles(self, scrips, a, b, interval="1minute"):
            return {s: idx for s in scrips if s.endswith("40000001")}
        def _get(self, path, params=None, expect_json=True):
            self.calls.append(path)
            if path.endswith("expired/expiries"):
                return {"status": "success", "data": [expiry.isoformat()]}
            if path.endswith("expired/contracts"):
                return {"status": "success", "data": [
                    {"trading_symbol": sym[s], "instrument_type": "OPTIDX", "strike_price": c.strike,
                     "option_type": c.opt_type, "lot_size": 65, "expiry": expiry.isoformat()}
                    for s, c in old.items()]}
            if "/historical/expired/" in path:
                want = params["trading-symbols"].split(",")
                return {"success": True, "data": {sym[s]: {"candles": opt[s]} for s in old if sym[s] in want}}
            raise AssertionError(path)

    R._EXPIRED.clear()
    c = cfg()
    c["trade"]["underlying"] = "NIFTY"
    trades, note = R.replay_day(c, Client(), [], day, log=lambda m: None)   # no live contracts at all
    assert note == "traded" and len(trades) == 1, note
    assert any("historical/expired" in p for p in Client.calls)


# ---------------- trend-following ----------------
def _with_previous_day(idx, base):
    prev = []
    for i, c in enumerate(idx):
        wob = 6 * math.sin(i / 3)
        prev.append({"ts": c["ts"] - 86400, "o": base + wob, "h": base + wob + 3, "l": base + wob - 3,
                     "c": base + wob, "v": 0})
    return prev + idx


def trend_cfg():
    c = cfg()
    c["strategy"]["name"] = "trend"
    return c


def test_trend_buys_calls_in_an_uptrend_and_trails():
    path = [25200.0 + 1.2 * i + 8 * math.sin(i / 6) for i in range(375)]
    day, idx, opt, contracts, expiry = make_day(path=path)
    logs = []
    trades = simulate_day(trend_cfg(), day, _with_previous_day(idx, 25200.0), opt, contracts, expiry, log=logs.append)
    assert trades, logs
    assert all(t.opt_type == "CE" for t in trades)
    assert len(trades) <= 3
    assert sum(t.net_pnl for t in trades) > 0
    assert any("stop moved" in m for m in logs)


def test_trend_buys_puts_in_a_downtrend():
    path = [25200.0 - 1.2 * i + 8 * math.sin(i / 6) for i in range(375)]
    day, idx, opt, contracts, expiry = make_day(path=path)
    trades = simulate_day(trend_cfg(), day, _with_previous_day(idx, 25200.0), opt, contracts, expiry, log=quiet())
    assert trades and all(t.opt_type == "PE" for t in trades)


def test_trend_stays_out_of_a_choppy_day():
    path = [25200.0 + 25 * math.sin(i / 4) for i in range(375)]
    day, idx, opt, contracts, expiry = make_day(path=path)
    assert simulate_day(trend_cfg(), day, _with_previous_day(idx, 25200.0), opt, contracts, expiry, log=quiet()) == []


def test_trailing_stop_never_moves_back():
    from bot.orb import Setup, spot_exit
    c = trend_cfg()
    s = Strategy(c, log=quiet())
    day, idx, opt, contracts, expiry = make_day(path=orb_path(up=True))
    p = s.open(contracts[0], 100.0, datetime(2026, 11, 19, 11, 0, tzinfo=IST), Setup("CE", 25000.0, 24970.0, 0.0, 30.0, "t"))
    assert spot_exit(c, p, {"h": 25020, "l": 25000, "c": 25015}, "trend") is None and p.spot_stop == 24970.0
    assert spot_exit(c, p, {"h": 25031, "l": 25010, "c": 25025}, "trend") is None and p.spot_stop == 25000.0  # +1R
    assert spot_exit(c, p, {"h": 25100, "l": 25060, "c": 25090}, "trend") is None
    assert p.spot_stop == 25100 - 45                                   # trails 1.5R behind the best
    assert spot_exit(c, p, {"h": 25080, "l": 25060, "c": 25070}, "trend") is None and p.spot_stop == 25055
    assert spot_exit(c, p, {"h": 25060, "l": 25040, "c": 25050}, "trend") == "TRAIL_STOP"


def test_daily_loss_limits_stop_new_trades():
    from bot.strategy import Trade
    s = Strategy(trend_cfg(), log=quiet())
    now = datetime(2026, 11, 19, 11, 0, tzinfo=IST)
    assert s.can_enter(now)
    s.closed = [Trade("2026-11-19", "X", "CE", 1, 65, "10:00", 1, "10:10", 1, "STOP_LOSS", -1, 0, -1000, "")] * 2
    assert not s.can_enter(now)   # 2 losing trades: done for the day
    s.closed = [Trade("2026-11-19", "X", "CE", 1, 65, "10:00", 1, "10:10", 1, "STOP_LOSS", -1, 0, -5200, "")]
    assert not s.can_enter(now)   # over the ₹5,000 daily loss limit


# ---------------- compare command (offline, from cached-style day data) ----------------
def test_compare_runs_every_version_and_the_walk_forward_check(tmp_path):
    import gzip
    import json
    from dataclasses import asdict
    from datetime import timedelta
    import run as R
    from bot import compare
    days = []
    for n in range(8):
        d = date(2026, 11, 2) + timedelta(days=n + 2 * (n // 5))
        kind = n % 4
        path = (orb_path(up=True) if kind == 0 else orb_path(up=False) if kind == 1 else
                orb_path(up=True, fail=True) if kind == 2 else [25200.0 + 25 * math.sin(i / 4) for i in range(375)])
        _, idx, opt, contracts, expiry = make_day(path=path, day=d)
        data = {"day": d.isoformat(), "index": _with_previous_day(idx, 25200.0), "note": None,
                "chains": {expiry.isoformat(): {"expired": False, "candles": opt,
                           "contracts": [dict(asdict(c), expiry=c.expiry.isoformat()) for c in contracts]}}}
        # round-trip through the cache format
        f = tmp_path / f"{d}.json.gz"
        with gzip.open(f, "wt") as fh:
            json.dump(data, fh)
        with gzip.open(f, "rt") as fh:
            days.append((d, json.load(fh)))
    logs = []
    rows = compare.compare(cfg(), days, R.run_day, log=logs.append)
    text = "\n".join(logs)
    assert "ORB as tested" in text and "Trend, 3 trades/day" in text and "honesty check" in text
    orb_whole = next(r for r in rows if r["version"].startswith("ORB as tested") and r["period"] == "whole")
    assert orb_whole["trades"] >= 4
