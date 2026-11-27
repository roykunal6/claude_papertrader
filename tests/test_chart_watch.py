import json
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.chart_watch import BarCloseTracker, ChartTrader, Readings, start_server  # noqa: E402
from bot.instruments import Contract  # noqa: E402
from bot.strategy import IST  # noqa: E402
from tests.test_rules import cfg  # noqa: E402

EXP = date(2026, 12, 1)


def at(hm, s=0):
    h, m = map(int, hm.split(":"))
    return datetime(2026, 11, 27, h, m, s, tzinfo=IST)


class Quiet:
    def __init__(self):
        self.sent = []

    def send(self, title, body, speak=None):
        self.sent.append(title)


def trader(prices, **over):
    c = cfg()
    c["chart_watch"].update(over)
    contracts = [Contract(f"NFO_{k}{t}", f"NIFTY{k}{t}", float(k), t, EXP, 65)
                 for k in range(25700, 26100, 50) for t in ("CE", "PE")]
    return ChartTrader(c, contracts, EXP, lambda s: prices.get(s), Quiet(), log=lambda *_: None)


def test_tracker_only_fires_on_closed_candle_change():
    t = BarCloseTracker()
    assert t.feed("BEARISH", at("09:20", 5)) is None
    assert t.feed("BEARISH", at("09:21", 1)) is None     # 09:20 closed red: first candle, no change
    assert t.feed("BULLISH", at("09:21", 30)) is None    # flickers blue inside the candle...
    assert t.feed("BEARISH", at("09:21", 55)) is None    # ...and back to red before it closes
    assert t.feed("BULLISH", at("09:22", 1)) is None     # 09:21 closed red: no change
    assert t.feed(None, at("09:22", 40)) is None         # chart briefly unreadable: keeps last colour
    assert t.feed("BULLISH", at("09:23", 0)) == ("BEARISH", "BULLISH")
    assert t.feed("BULLISH", at("09:24", 0)) is None


def test_blue_buys_itm_call_and_red_sells():
    prices = {"NFO_25850CE": 200.0}
    tr = trader(prices)
    tr.on_change("BEARISH", "BULLISH", 25880.0, at("10:00"))
    p = tr.position
    assert p and p.contract.strike == 25850 and p.contract.opt_type == "CE"
    assert p.qty == 2 * 65                       # ₹30k / (₹200.6 x 65) = 2 lots
    prices["NFO_25850CE"] = 230.0
    tr.on_price(230.0, at("10:05"))
    assert tr.position.stop > p.entry_price * 0.8  # trailing stop moved up
    tr.on_change("BULLISH", "BEARISH", 25850.0, at("10:30"))
    assert tr.position is None and tr.closed[0].exit_reason == "TREND_FLIP" and tr.closed[0].net_pnl > 0


def test_red_buys_put_only_when_asked():
    prices = {"NFO_25900PE": 150.0}
    tr = trader(prices)
    tr.on_change("BULLISH", "BEARISH", 25880.0, at("10:00"))
    assert tr.position is None
    tr = trader(prices, on_red="buy_pe")
    tr.on_change("BULLISH", "BEARISH", 25880.0, at("10:00"))
    assert tr.position.contract.opt_type == "PE" and tr.position.contract.strike == 25900


def test_stop_hours_and_daily_loss_limit():
    prices = {"NFO_25850CE": 200.0}
    tr = trader(prices, daily_loss_stop=1000)
    tr.on_change("BEARISH", "BULLISH", 25880.0, at("09:16"))
    assert tr.position is None                   # before 09:20
    tr.on_change("BEARISH", "BULLISH", 25880.0, at("10:00"))
    tr.on_price(150.0, at("10:10"))              # -25%: stop
    assert tr.closed[-1].exit_reason == "OPTION_STOP" and tr.done_for_day
    tr.on_change("BEARISH", "BULLISH", 25880.0, at("10:20"))
    assert tr.position is None                   # done for the day


def test_alerts_only_mode_and_square_off():
    prices = {"NFO_25850CE": 200.0}
    tr = trader(prices, auto_buy=False)
    tr.on_change("BEARISH", "BULLISH", 25880.0, at("10:00"))
    assert tr.position is None and any("TrendIkaterr" in s for s in tr.alert.sent)
    tr = trader(prices)
    tr.on_change("BEARISH", "BULLISH", 25880.0, at("14:00"))
    tr.on_price(205.0, at("15:15"))
    assert tr.closed[-1].exit_reason == "TIME_3:15"


def test_server_takes_readings_from_the_extension():
    r = Readings()
    srv = start_server(r, 0, log=lambda *_: None)
    port = srv.server_address[1]
    msg = {"panes": [{"pane": "NIFTY 50", "state": "BULLISH", "colour": "rgb(33, 150, 243)"}], "hovering": False}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/signal", data=json.dumps(msg).encode(),
                                 headers={"Content-Type": "application/json"})
    assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"]
    assert r.state("NIFTY 50") == "BULLISH" and r.state("other") is None
    srv.shutdown()
