"""Test several strategy versions on exactly the same days, and check them on months they weren't tuned on.

How the honesty check works (walk-forward):
  * The period is split in half: the EARLIER half and the LATER half.
  * For the trend strategy we try a small grid of settings, pick the one that did best on the earlier
    half only, and then report how that same pick did on the later half, which it never "saw".
  * If a version only looks good on the half it was picked on, it's luck, not an edge.
"""
from __future__ import annotations

import copy
import itertools
from datetime import date


def variants(base: dict) -> list[tuple[str, dict]]:
    def v(**changes):
        c = copy.deepcopy(base)
        for path, val in changes.items():
            sec, key = path.split("__")
            c.setdefault(sec, {})[key] = val
        return c

    out = [
        ("ORB as tested (no expiry days)", v(strategy__name="orb", trade__skip_expiry_day=True)),
        ("ORB + expiry days", v(strategy__name="orb", trade__skip_expiry_day=False)),
        ("ORB + trailing stop, no target", v(strategy__name="orb", trade__skip_expiry_day=False,
                                             orb__target_r=0, orb__trail_start_r=1.0, orb__trail_r=1.0)),
        ("Trend, 3 trades/day", v(strategy__name="trend")),
        ("Trend, 1 trade/day", v(strategy__name="trend", trend__max_trades_per_day=1)),
        ("Afternoon momentum alone", v(strategy__name="late")),
        ("ORB + afternoon momentum", v(strategy__name="orb+late")),
    ]
    return out


def trend_grid(base: dict) -> list[tuple[str, dict]]:
    out = []
    for adx, trail, stop in itertools.product((18, 22, 26), (1.0, 1.5, 2.5), (1.0, 1.5, 2.0)):
        c = copy.deepcopy(base)
        c["strategy"]["name"] = "trend"
        c["trend"].update(adx_min=adx, trail_r=trail, stop_atr=stop)
        out.append((f"Trend ADX>={adx} trail {trail}R stop {stop}xATR", c))
    return out


def late_grid(base: dict) -> list[tuple[str, dict]]:
    out = []
    for entry, morning, stop in itertools.product(("14:30", "14:45", "14:55"), (0.1, 0.2, 0.4), (20, 30, 50)):
        c = copy.deepcopy(base)
        c["strategy"]["name"] = "late"
        c["late"].update(entry_time=entry, min_morning_pct=morning, stop_pts=stop)
        out.append((f"Afternoon {entry} morning>={morning}% stop {stop}", c))
    return out


def run(cfg: dict, days: list[tuple[date, dict]], run_day) -> dict[str, list]:
    """[(day, trades, note)] for one config over the given days."""
    out = []
    for day, data in days:
        trades, note = run_day(cfg, data, day, log=lambda m: None)
        out.append((day, trades, note))
    return out


def stats(results: list, capital: float) -> dict:
    valid = [(tr, sum(t.net_pnl for t in tr)) for _, tr, note in results
             if not note.startswith(("no market", "skipped", "weekend"))]
    day_pnl = [p for _, p in valid]
    trades = [t for tr, _ in valid for t in tr]
    n_days = len(day_pnl)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    total = sum(day_pnl)
    run = peak = dd = 0.0
    streak = worst = 0
    for t in trades:
        run += t.net_pnl
        peak = max(peak, run)
        dd = min(dd, run - peak)
        streak = streak + 1 if t.net_pnl <= 0 else 0
        worst = max(worst, streak)
    traded_days = [p for tr, p in valid if tr]
    return {
        "days": n_days, "trades": len(trades),
        "days_traded_pct": 100 * len(traded_days) / n_days if n_days else 0,
        "win_pct": 100 * wins / len(trades) if trades else 0,
        "net": total, "per_day": total / n_days if n_days else 0,
        "green_days_pct": 100 * sum(1 for p in day_pnl if p > 0) / n_days if n_days else 0,
        "days_8pct": sum(1 for p in day_pnl if p >= 0.08 * capital),
        "max_dd": dd, "worst_streak": worst,
        "worst_day": min(day_pnl) if day_pnl else 0, "best_day": max(day_pnl) if day_pnl else 0,
    }


def split(results: list) -> tuple[list, list]:
    h = len(results) // 2
    return results[:h], results[h:]


HEAD = (f"{'Version':38} {'Trades':>6} {'Days%':>6} {'Win%':>5} {'Net ₹':>9} {'₹/day':>6} "
        f"{'Green%':>6} {'8%days':>6} {'MaxDD ₹':>9} {'LossRun':>7}")


def row(name: str, s: dict) -> str:
    return (f"{name[:38]:38} {s['trades']:>6} {s['days_traded_pct']:>5.0f}% {s['win_pct']:>4.0f}% "
            f"{s['net']:>9,.0f} {s['per_day']:>6,.0f} {s['green_days_pct']:>5.0f}% {s['days_8pct']:>6} "
            f"{s['max_dd']:>9,.0f} {s['worst_streak']:>7}")


def compare(cfg: dict, days: list[tuple[date, dict]], run_day, log=print) -> list[dict]:
    """Print the comparison and return rows for a CSV."""
    cap = cfg["capital"]["total"]
    first, last = split(days)
    rows = []
    if first and last:
        log(f"Earlier half: {first[0][0]} to {first[-1][0]}   Later half: {last[0][0]} to {last[-1][0]}")
    log("Days% = share of days with a trade. Green% = share of days that ended in profit. "
        f"8%days = days that made 8% of ₹{cap:,} (₹{0.08 * cap:,.0f}).")
    log("MaxDD = biggest drop from a high point. LossRun = longest run of losing trades.\n")

    def report(title, named):
        log(title)
        log(HEAD)
        for part, label in ((days, "whole"), (first, "earlier"), (last, "later")):
            for name, c in named:
                s = stats(run(c, part, run_day), cap)
                rows.append({"version": name, "period": label, **{k: round(v, 1) for k, v in s.items()}})
                log(row(f"{name} [{label}]", s))
            log("")

    report("=== The main versions, same days, same costs ===", variants(cfg))

    # walk-forward: pick settings on the earlier half, judge them on the later half
    for label, grid in (("Trend", trend_grid(cfg)), ("Afternoon momentum", late_grid(cfg))):
        scored = []
        for name, c in grid:
            s1 = stats(run(c, first, run_day), cap)
            scored.append((s1["net"], name, c, s1))
        scored.sort(key=lambda x: -x[0])
        log(f"=== {label} settings tried on the EARLIER half (best 5 of {len(grid)}) ===")
        log(HEAD)
        for _, name, _, s1 in scored[:5]:
            log(row(name, s1))
        _, best_name, best_cfg, _ = scored[0]
        s2 = stats(run(best_cfg, last, run_day), cap)
        log(f"\n=== Honesty check: the earlier half's best {label.lower()} settings, on the LATER half it never saw ===")
        log(HEAD)
        log(row(best_name, s2))
        log("")
        rows.append({"version": f"walk-forward pick: {best_name}", "period": "later",
                     **{k: round(v, 1) for k, v in s2.items()}})
    return rows
