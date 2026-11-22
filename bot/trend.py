"""Trend-following on the Nifty 50 index: trade only when the trend is clear, ride it with a trailing stop.

The whole strategy, in plain words (all on Nifty's 5-minute chart, built from 1-minute data):
  1. A CLEAR UPTREND means all of these at once:
       - the fast average (9 candles) is above the slow average (21 candles), and the slow one is rising
       - ADX, a standard "how strong is the trend" gauge, is at least 22
       - Nifty is above where it opened today
     A clear downtrend is the mirror image. Anything else: stay out.
  2. ENTRY, in a clear uptrend (buy a CALL; mirror for a PUT):
       - the trend has just become clear, or
       - Nifty dipped back to the fast average and the next candle closes above the previous candle's high
         (buying the dip inside a trend, not the top)
  3. STOP: 1.5x the recent average candle range (ATR) below the entry. Then:
       - after 1x that risk in profit, the stop moves to the entry level
       - the stop then trails 1.5x the risk behind the best price reached, and only ever moves up
       - exit early if the fast average crosses back under the slow one (the trend is over)
     No fixed target: winners are held as long as the trend and the trailing stop allow.
  4. Up to 3 trades a day, but stop for the day after 2 losing trades or ₹5,000 lost.
"""
from __future__ import annotations

from datetime import datetime

from .orb import IST, Setup, _dt, ema, five_minute, hhmm


def wilder(values: list[float], length: int) -> list[float]:
    out: list[float] = []
    for i, v in enumerate(values):
        if i == 0:
            out.append(v)
        else:
            out.append(out[-1] + (v - out[-1]) / length)
    return out


_IND: dict = {}


def indicators(bars: list[dict], t: dict) -> dict:
    """Cached: in a backtest the bars are beginnings of one primed series, and these indicators only look
    backwards, so the values for the whole series can be computed once and cut to length."""
    from . import orb
    key = (t["ema_fast"], t["ema_slow"], t["adx_len"], t["atr_len"])
    full = orb._PRIMED.get(bars[0]["ts"]) if bars else None
    if full is None or len(bars) > len(full) or full[len(bars) - 1]["ts"] != bars[-1]["ts"]:
        return _indicators(bars, t)
    k = (id(full), key)
    if k not in _IND:
        if len(_IND) > 64:
            _IND.clear()
        _IND[k] = _indicators(full, t)
    n = len(bars)
    return {name: vals[:n] for name, vals in _IND[k].items()}


def _indicators(bars: list[dict], t: dict) -> dict:
    """EMA fast/slow, ATR and ADX for a list of 5-min bars (oldest first)."""
    closes = [b["c"] for b in bars]
    fast, slow = ema(closes, t["ema_fast"]), ema(closes, t["ema_slow"])
    tr, pdm, ndm = [], [], []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["h"] - b["l"])
            pdm.append(0.0)
            ndm.append(0.0)
            continue
        p = bars[i - 1]
        tr.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))
        up, down = b["h"] - p["h"], p["l"] - b["l"]
        pdm.append(up if up > down and up > 0 else 0.0)
        ndm.append(down if down > up and down > 0 else 0.0)
    n = t["adx_len"]
    atr_s, pdm_s, ndm_s = wilder(tr, n), wilder(pdm, n), wilder(ndm, n)
    dx = []
    for a, p, m in zip(atr_s, pdm_s, ndm_s):
        pdi = 100 * p / a if a else 0.0
        ndi = 100 * m / a if a else 0.0
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0)
    adx = wilder(dx, n)
    atr = wilder(tr, t["atr_len"])
    return {"fast": fast, "slow": slow, "adx": adx, "atr": atr}


def trend_state(bars: list[dict], ind: dict, i: int, day_open: float, t: dict) -> int:
    """+1 clear uptrend, -1 clear downtrend, 0 no clear trend, at bar i."""
    if i < t["ema_slow"] + 3:
        return 0
    f, s, a, c = ind["fast"][i], ind["slow"][i], ind["adx"][i], bars[i]["c"]
    slope = s - ind["slow"][i - 3]
    if a < t["adx_min"]:
        return 0
    if f > s and slope > 0 and c > s and c > day_open:
        return 1
    if f < s and slope < 0 and c < s and c < day_open:
        return -1
    return 0


def find_setup(cfg: dict, index_closed: list[dict], now: datetime,
               seen: set | None = None) -> tuple[Setup | None, str | None]:
    t = cfg["trend"]
    day = now.astimezone(IST).date()
    today = [c for c in index_closed if _dt(c["ts"]).date() == day]
    if not today:
        return None, None
    bars = five_minute(index_closed)
    if len(bars) < 2:
        return None, None
    last = bars[-1]
    if not 0 <= now.timestamp() - last["end_ts"] < 300 or _dt(last["ts"]).date() != day:
        return None, None
    if seen is not None:
        if last["ts"] in seen:
            return None, None
        seen.add(last["ts"])
    if _dt(last["end_ts"]).time() <= hhmm(cfg["timing"]["no_entry_before"]):
        return None, None
    ind = indicators(bars, t)
    i = len(bars) - 1
    day_open = today[0]["o"]
    state = trend_state(bars, ind, i, day_open, t)
    if state == 0:
        return None, None
    sign = state
    prev_state = trend_state(bars, ind, i - 1, day_open, t)
    prev = bars[i - 1]
    fresh = prev_state != state
    dip = ((prev["l"] <= ind["fast"][i - 1]) if sign > 0 else (prev["h"] >= ind["fast"][i - 1]))
    resume = (last["c"] > prev["h"]) if sign > 0 else (last["c"] < prev["l"])
    why = None
    if fresh and t.get("enter_on_fresh_trend", True):
        why = "the trend just became clear"
    elif dip and resume:
        why = "Nifty dipped to its 9-candle average and is moving with the trend again"
    if not why:
        return None, None
    atr = ind["atr"][i]
    risk = t["stop_atr"] * atr
    if risk > t["max_risk_pts"]:
        return None, (f"{_dt(last['ts']):%H:%M} trend signal but the stop would be {risk:.0f} points away "
                      f"(max {t['max_risk_pts']}). Too jumpy, not taken.")
    risk = max(risk, t.get("min_risk_pts", 10))
    close = last["c"]
    stop = close - sign * risk
    target = close + sign * t["target_r"] * risk if t.get("target_r") else 0.0
    reason = (f"Clear {'up' if sign > 0 else 'down'}trend (ADX {ind['adx'][i]:.0f}, 9-avg "
              f"{ind['fast'][i]:.0f} {'above' if sign > 0 else 'below'} 21-avg {ind['slow'][i]:.0f}) and {why}. "
              f"Nifty {close:.2f}, stop {stop:.0f}, trailing.")
    return Setup("CE" if sign > 0 else "PE", close, round(stop, 2), round(target, 2), round(risk, 2), reason, "trend"), None


def trend_over(cfg: dict, pos, index_closed: list[dict], now: datetime) -> bool:
    """True on a just-finished 5-min candle where the fast average has crossed back against the trade."""
    t = cfg["trend"]
    if not t.get("exit_on_trend_end", True):
        return False
    bars = five_minute(index_closed)
    if len(bars) < t["ema_slow"] + 2 or not 0 <= now.timestamp() - bars[-1]["end_ts"] < 60:
        return False
    ind = indicators(bars, t)
    sign = 1 if pos.contract.opt_type == "CE" else -1
    return (ind["fast"][-1] - ind["slow"][-1]) * sign < 0
