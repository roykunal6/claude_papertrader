"""Afternoon momentum ("the last half hour follows the first"), on the Nifty 50 index.

Based on Gao, Han, Li and Zhou, "Market Intraday Momentum", Journal of Financial Economics 2018: the
market's move in the first half hour, and in the half hour before the last one, tends to continue
into the last half hour.

In plain words:
  1. Morning move: Nifty from yesterday's close to 09:45.
  2. Afternoon move: Nifty over the 30 minutes before the entry time (14:15 to 14:45 by default).
  3. At the entry time, if both moves point the same way and the morning move is big enough:
     buy the in-the-money CALL (both up) or PUT (both down).
  4. Exit at 15:15, or earlier if Nifty moves stop_pts against the trade. At most one such trade a day.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .orb import IST, Setup, _dt, hhmm


def in_window(cfg: dict, t) -> bool:
    """True during the 5 minutes starting at the entry time (one 5-min candle close)."""
    start = hhmm(cfg["late"]["entry_time"])
    end = (datetime.combine(datetime(2000, 1, 1), start) + timedelta(minutes=5)).time()
    return start <= t < end


def _close_at(candles: list[dict], day, t) -> float | None:
    """Close of the last 1-min candle of `day` that finished at or before time t."""
    best = None
    for c in candles:
        d = _dt(c["ts"] + 60)
        if d.date() == day and d.time() <= t:
            best = c["c"]
    return best


def find_setup(cfg: dict, index_closed: list[dict], now: datetime, seen=None) -> tuple[Setup | None, str | None]:
    o = cfg["late"]
    now = now.astimezone(IST)
    if not in_window(cfg, now.time()) or not index_closed:
        return None, None
    day = now.date()
    before = [c for c in index_closed if _dt(c["ts"]).date() < day]
    today = [c for c in index_closed if _dt(c["ts"]).date() == day]
    if not before or not today:
        return None, None
    prev_close = before[-1]["c"]
    entry_t = hhmm(o["entry_time"])
    look_from = (datetime.combine(day, entry_t) - timedelta(minutes=o["lookback_min"])).time()
    morning = _close_at(today, day, hhmm(o["morning_end"]))
    start = _close_at(today, day, look_from)
    spot = _close_at(today, day, entry_t)
    if None in (morning, start, spot):
        return None, None
    r1 = 100 * (morning / prev_close - 1)
    r2 = 100 * (spot / start - 1)
    tag = f"morning {r1:+.2f}%, last {o['lookback_min']} min {r2:+.2f}%"
    if abs(r1) < o["min_morning_pct"] or abs(r2) < o["min_afternoon_pct"] or r1 * r2 <= 0:
        return None, f"{entry_t:%H:%M} afternoon check: {tag}. Not a clear same-direction move, no trade."
    sign = 1 if r1 > 0 else -1
    risk = o["stop_pts"]
    stop = spot - sign * risk
    reason = (f"Afternoon momentum: Nifty {tag}, both {'up' if sign > 0 else 'down'}. "
              f"Nifty {spot:.2f}, stop {stop:.0f}, exit by {cfg['timing']['square_off']}.")
    return Setup("CE" if sign > 0 else "PE", spot, round(stop, 2), 0.0, float(risk), reason, "late"), None
