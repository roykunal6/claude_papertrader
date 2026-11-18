"""Made-up market data for tests and the demo. Not real prices."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

from bot.instruments import Contract
from bot.strategy import IST

STEP = 50
LOT = 65


def _bars(day: date, closes: list[float], start_id: int = 0) -> list[dict]:
    t0 = datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=15)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append({"ts": int((t0 + timedelta(minutes=i)).timestamp()), "o": o,
                    "h": max(o, c) + 0.5, "l": min(o, c) - 0.5, "c": c, "v": 1000})
        prev = c
    return out


def option_price(spot: float, strike: float, opt_type: str, minutes_left: float) -> float:
    intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    time_value = 60 * math.sqrt(max(minutes_left, 1) / 2000)
    return round(intrinsic + time_value, 2)


def make_day(seed: int = 1, path: list[float] | None = None, day: date = date(2026, 11, 19)):
    """Returns (day, index_candles, option_candles_by_scrip, contracts, expiry)."""
    rng = random.Random(seed)
    if path is None:  # drift down in the morning, then a steady rally after 10:30
        spot, path = 25200.0, []
        for i in range(375):
            drift = -0.6 if i < 75 else 1.1
            spot += drift + rng.gauss(0, 3)
            path.append(round(spot, 2))
    index_c = _bars(day, path)
    expiry = day + timedelta(days=5)
    contracts, opt_c = [], {}
    lo, hi = int(min(path) // STEP - 2) * STEP, int(max(path) // STEP + 3) * STEP
    sid = 1000
    for k in range(lo, hi + STEP, STEP):
        for t in ("CE", "PE"):
            sid += 1
            c = Contract(f"NFO_{sid}", f"NIFTY{k}{t}", float(k), t, expiry, LOT)
            contracts.append(c)
            closes = [option_price(s, k, t, 375 - i + 5 * 375) for i, s in enumerate(path)]
            opt_c[c.scrip] = _bars(day, closes)
    return day, index_c, opt_c, contracts, expiry
