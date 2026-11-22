"""Opening Range Breakout (ORB) on the Nifty 50 index, traded with an in-the-money option.

The whole strategy, in plain words:
  1. 09:15 to 09:45: just watch. Note the highest and lowest Nifty price. That's the "opening range".
  2. Skip the day if that range is too wide (a wild, gappy open) or too narrow (a dead market).
  3. From 09:45, look at each finished 5-minute Nifty candle:
       - it closes ABOVE the range high, and above its 20-candle average -> buy a CALL (CE)
       - it closes BELOW the range low,  and below its 20-candle average -> buy a PUT  (PE)
     Don't chase: if price is already far past the range, skip it.
  4. Exits are decided on Nifty itself (not the jumpy option chart):
       - stop: Nifty closes back past the middle of the range (the breakout failed)
       - once Nifty has moved 1x the risk in our favour, the stop moves to the entry level
       - target: Nifty moves 2x the risk in our favour
       - safety net: the option itself falls 25%
       - 15:15: close whatever is open
  5. One trade a day, never on expiry day.

Signals use the INDEX's 5-minute candles (built from 1-minute data) because the option's 1-minute
chart is noisy: that noise is what caused the 1-7 minute whipsaw exits of the old trend-line rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN = time(9, 15)


def hhmm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, IST)


def ema(values: list[float], length: int) -> list[float]:
    out: list[float] = []
    k = 2.0 / (length + 1)
    for i, v in enumerate(values):
        out.append(v if i == 0 else out[-1] + k * (v - out[-1]))
    return out


_PRIMED: dict = {}  # first candle ts -> 5-min bars of a whole replayed series (speeds up backtests)


def prime(candles: list[dict]) -> None:
    """Backtests: build the 5-min bars of a whole series once. Later five_minute() calls on any
    beginning of that same series are answered from it instead of being rebuilt every minute."""
    _PRIMED.clear()
    if candles:
        _PRIMED[candles[0]["ts"]] = _five_minute(candles)
        _PRIMED["src"] = {c["ts"]: c["c"] for c in candles}


def five_minute(candles: list[dict]) -> list[dict]:
    if (candles and candles[0]["ts"] in _PRIMED
            and _PRIMED["src"].get(candles[-1]["ts"]) == candles[-1]["c"] and len(candles) > 1
            and _PRIMED["src"].get(candles[-2]["ts"]) == candles[-2]["c"]):
        full = _PRIMED[candles[0]["ts"]]
        upto = candles[-1]["ts"] + 60
        n = len(full)
        while n and full[n - 1]["ts"] + 300 > upto:
            n -= 1
        return full[:n]
    return _five_minute(candles)


def _five_minute(candles: list[dict]) -> list[dict]:
    """Group 1-min candles into 5-min candles aligned to 09:15 (09:15-09:20, 09:20-09:25, ...).
    Give it only finished 1-min candles; a 5-min candle is returned only once all its minutes are in
    (or a later minute exists, in case a minute is missing from the data)."""
    buckets: dict[tuple, list[dict]] = {}
    for c in candles:
        d = _dt(c["ts"])
        mins = (d.hour * 60 + d.minute) - (OPEN.hour * 60 + OPEN.minute)
        if mins < 0:
            continue
        buckets.setdefault((d.date(), mins // 5), []).append(c)
    keys = sorted(buckets)
    out = []
    for i, k in enumerate(keys):
        group = sorted(buckets[k], key=lambda c: c["ts"])
        last_min = (_dt(group[-1]["ts"]).hour * 60 + _dt(group[-1]["ts"]).minute
                    - (OPEN.hour * 60 + OPEN.minute)) % 5
        complete = last_min == 4 or i < len(keys) - 1
        if not complete:
            continue
        out.append({"ts": group[0]["ts"], "o": group[0]["o"], "h": max(c["h"] for c in group),
                    "l": min(c["l"] for c in group), "c": group[-1]["c"], "end_ts": group[-1]["ts"] + 60})
    return out


@dataclass
class Range:
    high: float
    low: float

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def width(self) -> float:
        return self.high - self.low


def opening_range(today: list[dict], range_end: time) -> Range | None:
    bars = [c for c in today if OPEN <= _dt(c["ts"]).time() < range_end]
    if not bars:
        return None
    return Range(high=max(c["h"] for c in bars), low=min(c["l"] for c in bars))


@dataclass
class Setup:
    """A breakout signal on Nifty. All prices are Nifty index points."""
    opt_type: str        # "CE" or "PE"
    spot: float          # Nifty at entry (close of the breakout 5-min candle)
    stop: float          # Nifty level that says "breakout failed"
    target: float        # Nifty level to book profit
    risk_pts: float      # distance from entry to stop
    reason: str


def range_problem(rng: Range, o: dict) -> str | None:
    pct = 100 * rng.width / rng.mid
    if pct > o["max_range_pct"]:
        return (f"opening range is {rng.width:.0f} points ({pct:.2f}%), wider than {o['max_range_pct']}%. "
                "Too wild today, no trade.")
    if pct < o["min_range_pct"]:
        return (f"opening range is only {rng.width:.0f} points ({pct:.2f}%), under {o['min_range_pct']}%. "
                "Too quiet today, no trade.")
    return None


def find_setup(cfg: dict, index_closed: list[dict], now: datetime,
               seen: set | None = None) -> tuple[Setup | None, str | None]:
    """(setup, note). Call once per finished 1-min candle; acts only on a 5-min candle that finished in the
    last 5 minutes and hasn't been looked at yet (seen holds the ones already checked).
    note explains a skip worth logging (or None)."""
    o = cfg["orb"]
    day = now.astimezone(IST).date()
    today = [c for c in index_closed if _dt(c["ts"]).date() == day]
    rng = opening_range(today, hhmm(o["range_end"]))
    if rng is None or not today:
        return None, None
    problem = range_problem(rng, o)
    if problem:
        return None, problem
    bars = five_minute(index_closed)
    if not bars:
        return None, None
    last = bars[-1]
    # act only on the 5-min candle that just finished, and only after the range is complete
    if not 0 <= now.timestamp() - last["end_ts"] < 300 or _dt(last["ts"]).date() != day:
        return None, None
    if seen is not None:
        if last["ts"] in seen:
            return None, None
        seen.add(last["ts"])
    if _dt(last["ts"]).time() < hhmm(o["range_end"]):
        return None, None
    avg = ema([b["c"] for b in bars], o["ema_length"])[-1] if o.get("use_ema_filter", True) else None
    buf = o["breakout_buffer_pts"]
    close = last["c"]
    if close > rng.high + buf:
        opt, sign = "CE", 1
    elif close < rng.low - buf:
        opt, sign = "PE", -1
    else:
        return None, None
    if avg is not None and (close - avg) * sign <= 0:
        return None, (f"{_dt(last['ts']):%H:%M} breakout {'up' if sign > 0 else 'down'} but Nifty is on the wrong "
                      f"side of its 20-candle average ({avg:.0f}). Not taken.")
    stop = rng.mid if o.get("stop_at", "mid") == "mid" else (rng.low if sign > 0 else rng.high)
    risk = abs(close - stop)
    if risk > o["max_risk_pts"]:
        return None, (f"{_dt(last['ts']):%H:%M} breakout {'up' if sign > 0 else 'down'} but Nifty is already "
                      f"{risk:.0f} points from the stop (max {o['max_risk_pts']}). Too late to chase.")
    target = close + sign * o["target_r"] * risk
    reason = (f"Nifty 5-min close {close:.2f} broke the opening range "
              f"{'high ' + format(rng.high, '.2f') if sign > 0 else 'low ' + format(rng.low, '.2f')}"
              f" (range {rng.low:.0f}-{rng.high:.0f}). Nifty stop {stop:.0f}, target {target:.0f}.")
    return Setup(opt, close, round(stop, 2), round(target, 2), round(risk, 2), reason), None


def spot_exit(cfg: dict, pos, bar: dict, section: str = "orb") -> str | None:
    """Check the latest finished 1-min Nifty candle against the trade's Nifty levels.

    Order: stop (on the candle's close), then target (if any), then move the stop:
      * after breakeven_r x risk in profit: stop to the entry level
      * after trail_start_r x risk in profit: stop trails trail_r x risk behind the best price (if trail_r > 0)
    The stop never moves backwards. Returns an exit reason or None."""
    if not pos or not pos.spot_entry:
        return None
    o = cfg[section]
    sign = 1 if pos.contract.opt_type == "CE" else -1
    if (bar["c"] - pos.spot_stop) * sign <= 0:
        if (pos.spot_stop - pos.spot_entry) * sign > 0.5:
            return "TRAIL_STOP"
        return "BREAKEVEN_STOP" if pos.spot_stop_moved else "STOP_LOSS"
    best = bar["h"] if sign > 0 else bar["l"]
    if pos.spot_target and (best - pos.spot_target) * sign >= 0:
        return "TARGET"
    pos.spot_best = best if not pos.spot_best else (max(pos.spot_best, best) if sign > 0 else min(pos.spot_best, best))
    gain = (pos.spot_best - pos.spot_entry) * sign
    new = pos.spot_stop
    if gain >= o.get("breakeven_r", 1.0) * pos.risk_pts:
        new = pos.spot_entry if sign > 0 and new < pos.spot_entry or sign < 0 and new > pos.spot_entry else new
    if o.get("trail_r", 0) and gain >= o.get("trail_start_r", 1.0) * pos.risk_pts:
        trail = pos.spot_best - sign * o["trail_r"] * pos.risk_pts
        if (trail - new) * sign > 0:
            new = trail
    if (new - pos.spot_stop) * sign > 0:
        pos.spot_stop = round(new, 2)
        pos.spot_stop_moved = True
    return None


def is_five_min_close(now: datetime) -> bool:
    t = now.astimezone(IST)
    return ((t.hour * 60 + t.minute) - (OPEN.hour * 60 + OPEN.minute)) % 5 == 0

