"""Replay a past day minute by minute from 1-min candles, using the same rules as live."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .instruments import Contract, itm_strikes, pick, strikes_for
from .strategy import IST, Strategy, Trade, hhmm


def closed_upto(candles: list[dict], close_ts: int) -> list[dict]:
    """Candles whose 1-min bar has finished by close_ts (epoch seconds)."""
    return [c for c in candles if c["ts"] + 60 <= close_ts]


def simulate_day(cfg: dict, day: date, index_candles: list[dict],
                 option_candles: dict[str, list[dict]], contracts: list[Contract],
                 expiry: date, log=print) -> list[Trade]:
    """index_candles / option_candles may include earlier days (used to warm up the indicator)."""
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

    locked: dict[str, float] = {}
    for bar in today:
        close_ts = bar["ts"] + 60
        now = datetime.fromtimestamp(close_ts, IST)

        if strat.position:
            p = strat.position
            held = option_candles.get(p.contract.scrip, [])
            this = next((c for c in held if c["ts"] == bar["ts"]), None)
            if this:
                reason = strat.check_price(this["l"])   # cautious: assume the low came first
                if reason in ("STOP_LOSS", "TRAIL_STOP"):
                    strat.close(min(p.stop, this["o"]), now, reason, exact=True)
                elif strat.check_price(this["h"]) == "TARGET":
                    strat.close(max(p.target, this["o"]), now, "TARGET", exact=True)
                else:
                    msg = strat.update_trail(this["h"])
                    if msg:
                        log(f"{now:%H:%M} {msg}")
                    if strat.red_line_exit(closed_upto(held, close_ts)):
                        strat.close(this["c"], now, "RED_LINE")
            if strat.position and now.time() >= square_off:
                last = closed_upto(held, close_ts)
                strat.close(last[-1]["c"] if last else p.entry_price, now, "TIME_3:15")
            continue

        if now.time() >= square_off:
            break
        if not strat.can_enter(now):
            continue
        mode = cfg["trade"].get("strike_mode", "follow")
        ce_k, pe_k = strikes_for(bar["c"], step, mode)
        if mode in ("lock", "single"):
            if not locked:
                locked.update(CE=ce_k, PE=pe_k)
                log(f"{now:%H:%M} Strike locked for the day: {ce_k:.0f} CE / {pe_k:.0f} PE (Nifty {bar['c']:.2f})")
            ce_k, pe_k = locked["CE"], locked["PE"]
        options = {}
        for opt_type, k in (("CE", ce_k), ("PE", pe_k)):
            c = pick(contracts, expiry, k, opt_type)
            if c and c.scrip in option_candles:
                options[opt_type] = (c, closed_upto(option_candles[c.scrip], close_ts))
        sig = strat.entry_signal(now, closed_upto(index_candles, close_ts), options)
        if sig:
            contract, reason = sig
            strat.open(contract, options[contract.opt_type][1][-1]["c"], now, reason)

    if strat.trades_today == 0:
        log(f"{day}: no clear signal after {cfg['timing']['no_entry_before']}. No trade.")
    return strat.closed


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
