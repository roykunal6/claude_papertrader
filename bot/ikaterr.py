"""TrendIkaterr auto-buy: buy every time the trend line turns BLUE, ride it with a trailing stop.

TrendIkaterr itself is a closed INDmoney indicator, so this uses the same look-alike as before: an
11-candle average (EMA) of Nifty's 1-minute closes, coloured
  * BLUE when Nifty closes above the line and the line is rising
  * RED  when Nifty closes below the line and the line is falling
  * otherwise it keeps its last colour (so it doesn't flicker).

The whole strategy, in plain words:
  1. BUY signal = the line turns BLUE on a finished candle -> buy the in-the-money CALL (CE).
     When the line turns RED, that is the PUT's buy signal -> buy the in-the-money PUT (PE)
     (switch off with buy_puts = false).
  2. Spend up to ₹30,000 a trade (max_invested). Every signal can be a new trade, one at a time.
  3. Exits, checked every second on the option price:
       - stop: the option falls stop_pct (7%) below what we paid
       - once it's up breakeven_at_pct (5%), the stop moves to our buy price
       - then the stop follows trail_pct (7%) below the highest price so far, and only moves up;
         past tight_after_pct (15%) it follows tight_trail_pct (4%) below the high.
         There is no fixed target: the stop keeps pushing up behind the price.
       - the line turns the other colour (the signal is over)
       - the day's profit (closed trades + this one) reaches day_target (₹5,000): sell, done for the day
       - the day's loss reaches max_daily_loss (₹3,000): sell, done for the day
       - 15:15: sell whatever is open
"""
from __future__ import annotations

from datetime import datetime

from .orb import IST, Setup, _dt, ema, five_minute

BLUE, RED = "blue", "red"
LOOKBACK = 300  # candles used for the line; an 11-candle average forgets anything older


def bars_for(cfg: dict, index_closed: list[dict]) -> list[dict]:
    """The candles the line is drawn on: Nifty 1-min (default) or 5-min."""
    if cfg["ikaterr"].get("timeframe_min", 1) == 5:
        return five_minute(index_closed)[-LOOKBACK:]
    return index_closed[-LOOKBACK:]


def colours(bars: list[dict], length: int) -> tuple[list[float], list[str | None]]:
    closes = [b["c"] for b in bars]
    line = ema(closes, length)
    out: list[str | None] = []
    colour = None
    for i, (c, l) in enumerate(zip(closes, line)):
        if i >= length:  # warm-up: don't colour the first `length` candles
            if c > l and l > line[i - 1]:
                colour = BLUE
            elif c < l and l < line[i - 1]:
                colour = RED
        out.append(colour)
    return line, out


def _fresh(cfg: dict, bars: list[dict], now: datetime) -> bool:
    """True if the last bar finished in the last 2 minutes (entries are acted on once, via `seen`)."""
    if not bars:
        return False
    end = bars[-1].get("end_ts", bars[-1]["ts"] + 60)
    return 0 <= now.timestamp() - end < 120  # a little slack in case the candle arrives late (live)


def last_flip(cfg: dict, index_closed: list[dict], now: datetime) -> tuple[str | None, float, float]:
    """(colour it just flipped to or None, Nifty close, line value) for the bar that just finished."""
    bars = bars_for(cfg, index_closed)
    length = cfg["ikaterr"].get("length", 11)
    if len(bars) < length + 2 or not _fresh(cfg, bars, now):
        return None, 0.0, 0.0
    line, col = colours(bars, length)
    if col[-2] is None or col[-1] == col[-2]:
        return None, bars[-1]["c"], line[-1]
    return col[-1], bars[-1]["c"], line[-1]


def find_setup(cfg: dict, index_closed: list[dict], now: datetime,
               seen: set | None = None) -> tuple[Setup | None, str | None]:
    flip, close, line = last_flip(cfg, index_closed, now)
    if flip is None:
        return None, None
    if _dt(index_closed[-1]["ts"]).date() != now.astimezone(IST).date():
        return None, None
    if seen is not None:
        key = ("ik", index_closed[-1]["ts"])
        if key in seen:
            return None, None
        seen.add(key)
    if flip == RED and not cfg["ikaterr"].get("buy_puts", True):
        return None, f"{now:%H:%M} line turned RED (a PUT signal) but buy_puts is off. Not taken."
    opt = "CE" if flip == BLUE else "PE"
    tf = cfg["ikaterr"].get("timeframe_min", 1)
    reason = (f"TrendIkaterr look-alike turned {flip.upper()} on Nifty's {tf}-min chart "
              f"(close {close:.2f} {'above' if flip == BLUE else 'below'} the line {line:.2f}).")
    return Setup(opt, close, 0.0, 0.0, 0.0, reason, "ikaterr"), None


def flipped_against(cfg: dict, pos, index_closed: list[dict], now: datetime) -> bool:
    """True if the line has just turned against the open trade (RED for a CALL, BLUE for a PUT)."""
    if not cfg["ikaterr"].get("exit_on_flip", True):
        return False
    flip, _, _ = last_flip(cfg, index_closed, now)
    return flip == (RED if pos.contract.opt_type == "CE" else BLUE)
