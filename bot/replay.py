"""Replay a past day minute by minute from 1-min candles, using the same rules as live."""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta

from .instruments import Contract, itm_strikes, pick, pick_strike
from .orb import is_five_min_close, prime
from .strategy import IST, Strategy, Trade, hhmm


_TS: dict = {}


def closed_upto(candles: list[dict], close_ts: int) -> list[dict]:
    """Candles whose 1-min bar has finished by close_ts (epoch seconds). Candles are oldest first."""
    if len(candles) < 50:
        return [c for c in candles if c["ts"] + 60 <= close_ts]
    key = id(candles)
    ts = _TS.get(key)
    if ts is None or len(ts[1]) != len(candles) or ts[0] is not candles:
        if len(_TS) > 256:
            _TS.clear()
        ts = _TS[key] = (candles, [c["ts"] for c in candles])
    return candles[:bisect_right(ts[1], close_ts - 60)]


def simulate_day(cfg: dict, day: date, index_candles: list[dict],
                 option_candles: dict[str, list[dict]], contracts: list[Contract],
                 expiry: date, log=print) -> list[Trade]:
    """index_candles may include earlier days (used to warm up the 20-candle average)."""
    strat = Strategy(cfg, log=log)
    if cfg["trade"]["skip_expiry_day"] and day == expiry:
        log(f"{day} is a Nifty expiry day. No trading.")
        return []
    step = cfg["trade"]["strike_step"]
    square_off = hhmm(cfg["timing"]["square_off"])
    today = [c for c in index_candles
             if datetime.fromtimestamp(c["ts"], IST).date() == day]
    if not today:
        log(f"No Nifty candles for {day} (holiday or no data).")
        return []

    prime(index_candles)
    for bar in today:
        close_ts = bar["ts"] + 60
        now = datetime.fromtimestamp(close_ts, IST)

        if strat.position and strat.position.kind == "ikaterr":
            _ikaterr_bar(strat, bar, option_candles, index_candles, close_ts, now, square_off)
            if strat.position or not strat.every_minute:
                continue
        elif strat.position:
            p = strat.position
            held = option_candles.get(p.contract.scrip, [])
            this = next((c for c in held if c["ts"] == bar["ts"]), None)
            if this and strat.check_price(this["l"]):   # cautious: assume the option's low came first
                strat.close(min(p.stop, this["o"]), now, "OPTION_STOP", exact=True)
            else:
                why = strat.index_exit(bar, closed_upto(index_candles, close_ts) if is_five_min_close(now) else None, now)
                if why:
                    last = closed_upto(held, close_ts)
                    strat.close(this["c"] if this else (last[-1]["c"] if last else p.entry_price), now, why)
            if strat.position and now.time() >= square_off:
                last = closed_upto(held, close_ts)
                strat.close(last[-1]["c"] if last else p.entry_price, now, "TIME_3:15")
            continue

        if now.time() >= square_off:
            break
        if not is_five_min_close(now) and not strat.every_minute:  # signals only come on finished 5-min candles
            continue
        setup = strat.entry_signal(now, closed_upto(index_candles, close_ts))
        if not setup:
            continue
        k = pick_strike(setup.spot, setup.opt_type, step, cfg["trade"].get("strike_mode", "itm"))
        c = pick(contracts, expiry, k, setup.opt_type)
        bars = closed_upto(option_candles.get(c.scrip, []), close_ts) if c else []
        if not bars or bars[-1]["ts"] < bar["ts"] - 300:
            strat.use_signal(setup.kind)
            log(f"{now:%H:%M} Signal for {k:.0f} {setup.opt_type} but no recent price for it in the data. Skipped.")
            continue
        strat.open(c, bars[-1]["c"], now, setup)

    if strat.trades_today == 0:
        log(f"{day}: no clean signal after {cfg['timing']['no_entry_before']}. No trade.")
    return strat.closed


def _ikaterr_bar(strat: Strategy, bar: dict, option_candles: dict, index_candles: list[dict],
                 close_ts: int, now: datetime, square_off) -> None:
    """One finished minute with a TrendIkaterr trade open. Cautious order inside the candle:
    the low first (stop, day loss limit), then the high (pushes the stop up, day profit target),
    then the close (line turned against us), then the 15:15 square-off."""
    p = strat.position
    held = option_candles.get(p.contract.scrip, [])
    this = next((c for c in held if c["ts"] == bar["ts"]), None)
    if this:
        hit = strat.low_exit(this["l"])
        if hit:
            strat.close(min(hit[1], this["o"]), now, hit[0], exact=True)
            return
        strat.trail(this["h"])
        hit = strat.high_exit(this["h"])
        if hit:
            strat.close(max(hit[1], this["o"]), now, hit[0])
            return
    why = strat.index_exit(bar, closed_upto(index_candles, close_ts), now)
    if not why and now.time() >= square_off:
        why = "TIME_3:15"
    if why:
        last = closed_upto(held, close_ts)
        strat.close(this["c"] if this else (last[-1]["c"] if last else p.entry_price), now, why)


def strikes_needed(index_candles_today: list[dict], step: int) -> set[float]:
    """Every ITM strike the bot might look at during the day."""
    ks: set[float] = set()
    for c in index_candles_today:
        for price in (c["h"], c["l"], c["c"]):
            ce, pe = itm_strikes(price, step)
            ks.update({ce, pe, float(round(price / step) * step)})
    return ks


def day_window_ms(day: date, warmup_days: int = 4) -> tuple[int, int]:
    start = datetime.combine(day - timedelta(days=warmup_days), datetime.min.time(), IST)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), IST)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
