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
from bot.indicator import BLUE, RED, trend_line  # noqa: E402
from bot.instruments import itm_strikes, load_nifty_options  # noqa: E402
from bot.replay import simulate_day  # noqa: E402
from bot.strategy import IST, Strategy  # noqa: E402
from tests.fake_market import make_day  # noqa: E402


def cfg():
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def test_trend_line_flips():
    closes = [100 - i for i in range(30)] + [70 + 2 * i for i in range(30)]
    pts = trend_line(closes, 11)
    assert pts[25].colour == RED
    assert pts[-1].colour == BLUE
    buys = [i for i, p in enumerate(pts) if p.signal == "BUY"]
    assert len(buys) == 1 and buys[0] > 30


def test_itm_strikes():
    assert itm_strikes(25227.95, 50) == (25200.0, 25250.0)
    assert itm_strikes(25200.0, 50) == (25150.0, 25250.0)  # exactly at a strike is not ITM


def test_size_fits_in_30k():
    s = Strategy(cfg())
    assert s.size(150.0, 65) == 195          # 3 lots = ₹29,250
    assert s.size(175.0, 65) == 130          # 2 lots = ₹22,750
    assert s.size(500.0, 65) == 0            # one lot = ₹32,500 > ₹30,000


def test_charges_reasonable():
    buy = order_charges(30000, "BUY", cfg()["charges"])
    sell = order_charges(30000, "SELL", cfg()["charges"])
    assert 10 < buy < 30 and 35 < sell < 60


def test_no_entry_before_945_and_one_trade_only():
    day, idx, opt, contracts, expiry = make_day(seed=7)
    logs = []
    trades = simulate_day(cfg(), day, idx, opt, contracts, expiry, log=logs.append)
    assert len(trades) <= 1
    for t in trades:
        assert t.entry_time >= "09:45"
        assert t.exit_time <= "15:16"
        assert t.qty * t.entry_price <= 30000
        assert t.exit_reason in {"TARGET", "STOP_LOSS", "RED_LINE", "TIME_3:15"}


def test_direction_follows_nifty():
    # morning sell-off: should buy a PUT; steady rally from the open: should buy a CALL
    down = [25300.0 - 1.5 * i for i in range(375)]
    up = [25000.0 + 1.5 * i for i in range(375)]
    for path, want in ((down, "PE"), (up, "CE")):
        day, idx, opt, contracts, expiry = make_day(path=path)
        trades = simulate_day(cfg(), day, idx, opt, contracts, expiry, log=lambda m: None)
        assert all(t.opt_type == want for t in trades)


def test_expiry_day_skipped():
    day, idx, opt, contracts, expiry = make_day(seed=7)
    logs = []
    assert simulate_day(cfg(), day, idx, opt, contracts, day, log=logs.append) == []
    assert "expiry" in logs[0]


def test_flat_day_no_trade():
    flat = [25200.0 + (0.5 if i % 2 else -0.5) for i in range(375)]
    day, idx, opt, contracts, expiry = make_day(path=flat)
    assert simulate_day(cfg(), day, idx, opt, contracts, expiry, log=lambda m: None) == []


def test_target_and_stop_levels():
    c = cfg()
    day, idx, opt, contracts, expiry = make_day(seed=7)
    s = Strategy(c, log=lambda m: None)
    now = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    p = s.open(contracts[0], 100.0, now, "test")
    assert abs(p.target - p.entry_price * 1.15) < 0.03
    assert abs(p.stop - p.entry_price * 0.93) < 0.03
    assert s.check_price(p.target) is None          # trail mode: keep riding past +15%
    s.cfg = {**c, "trail": {**c["trail"], "target_mode": "exit"}}
    assert s.check_price(p.target) == "TARGET"
    assert s.check_price(p.stop) == "STOP_LOSS"
    assert s.check_price(p.entry_price) is None
    assert not s.can_enter(now)  # one trade a day


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


def test_trailing_stop_only_moves_up():
    c = cfg()
    day, idx, opt, contracts, expiry = make_day(seed=7)
    s = Strategy(c, log=lambda m: None)
    now = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    p = s.open(contracts[0], 100.0, now, "test")
    entry = p.entry_price
    assert s.update_trail(entry * 1.03) is None               # +3%: stop unchanged
    assert s.update_trail(entry * 1.06) is not None           # +6%: stop to breakeven or better
    assert p.stop >= entry
    s.update_trail(entry * 1.12)                              # +12%: stop 7% under the high
    assert abs(p.stop - entry * 1.12 * 0.93) < 0.1
    before = p.stop
    s.update_trail(entry * 1.01)                              # price falls back: stop stays
    assert p.stop == before
    assert s.check_price(before - 0.05) == "TRAIL_STOP"
    s.update_trail(entry * 1.20)                              # past +15%: tighter 4% gap
    assert abs(p.stop - entry * 1.20 * 0.96) < 0.1


def test_strike_locked_for_the_day():
    up = [25000.0 + 1.5 * i for i in range(375)]           # Nifty climbs 560 points
    day, idx, opt, contracts, expiry = make_day(path=up)
    logs = []
    trades = simulate_day(cfg(), day, idx, opt, contracts, expiry, log=logs.append)
    locked = [m for m in logs if "locked" in m]
    assert len(locked) == 1
    for t in trades:
        assert f"{t.strike:.0f} {t.opt_type}" in locked[0]
