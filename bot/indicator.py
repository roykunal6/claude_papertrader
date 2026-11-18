"""Look-alike of the TrendIkaterr trend line.

TrendIkaterr is a closed INDstocks indicator, so this is an approximation:
a moving average of the close (EMA or Hull MA, length 11 by default) that is
coloured BLUE when the trend is up and RED when it is down.

Colour rule (with memory, so the colour doesn't flicker):
  * BLUE when the close is above the line AND the line is rising
  * RED  when the close is below the line AND the line is falling
  * otherwise keep the previous colour

A BUY signal is the candle where the colour flips to BLUE.
A SELL signal is the candle where the colour flips to RED.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

BLUE = "blue"
RED = "red"


def ema(values: list[float], length: int) -> list[float]:
    out: list[float] = []
    k = 2.0 / (length + 1)
    for i, v in enumerate(values):
        out.append(v if i == 0 else out[-1] + k * (v - out[-1]))
    return out


def wma(values: list[float], length: int) -> list[float]:
    out: list[float] = []
    weights = list(range(1, length + 1))
    for i in range(len(values)):
        window = values[max(0, i - length + 1): i + 1]
        w = weights[-len(window):]
        out.append(sum(a * b for a, b in zip(window, w)) / sum(w))
    return out


def hma(values: list[float], length: int) -> list[float]:
    half = max(1, length // 2)
    root = max(1, int(math.sqrt(length)))
    w_half = wma(values, half)
    w_full = wma(values, length)
    diff = [2 * a - b for a, b in zip(w_half, w_full)]
    return wma(diff, root)


@dataclass
class TrendPoint:
    line: float
    colour: str | None   # BLUE, RED, or None while warming up
    signal: str | None   # "BUY", "SELL" or None


def trend_line(closes: list[float], length: int = 11, method: str = "ema") -> list[TrendPoint]:
    if method == "hma":
        line = hma(closes, length)
    elif method == "ema":
        line = ema(closes, length)
    else:
        raise ValueError(f"unknown indicator method {method!r}")

    points: list[TrendPoint] = []
    colour: str | None = None
    for i, (c, l) in enumerate(zip(closes, line)):
        prev_colour = colour
        if i >= length:  # warm-up: don't colour the first `length` candles
            rising = l > line[i - 1]
            falling = l < line[i - 1]
            if c > l and rising:
                colour = BLUE
            elif c < l and falling:
                colour = RED
        signal = None
        if prev_colour is not None and colour != prev_colour:
            signal = "BUY" if colour == BLUE else "SELL"
        points.append(TrendPoint(line=l, colour=colour, signal=signal))
    return points
