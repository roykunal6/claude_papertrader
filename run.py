"""Nifty options paper-trading bot.

  python run.py check                 test your API token and data access
  python run.py paper                 run today's live paper trading (start before 09:15)
  python run.py replay 2026-11-19     replay a past day with the same rules
  python run.py backtest 2026-11-16 2026-11-20   replay every day in a date range, with a summary
  python run.py demo                  run on a made-up day, no API needed
"""
from __future__ import annotations

import os
import sys
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.9 / 3.10 (e.g. macOS's built-in python3)
    import tomli as tomllib
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def cmd_check(cfg: dict) -> None:
    from bot.indstocks import IndstocksClient
    from bot.instruments import load_nifty_options, nearest_expiry
    from bot.strategy import IST
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    client.profile()
    print("1. Token works.")
    contracts = load_nifty_options(client.fno_instruments_csv(), cfg["trade"]["underlying"])
    today = datetime.now(IST).date()
    exp = nearest_expiry(contracts, today)
    print(f"2. Found {len(contracts)} Nifty option contracts. Nearest expiry: {exp}. "
          f"Lot size: {contracts[0].lot_size if contracts else '?'}")
    idx = cfg["api"]["index_scrip"]
    try:
        print(f"3. Nifty index ({idx}) last price: {client.ltp([idx]).get(idx)}")
    except Exception as e:  # noqa: BLE001
        print(f"3. PROBLEM reading the Nifty index with code {idx}: {e}")
    end = int(datetime.now(IST).timestamp() * 1000)
    got = client.candles([idx], end - 4 * 86400 * 1000, end)
    print(f"4. Got {len(got.get(idx, []))} one-minute Nifty candles for the last 4 days.")
    print("All good." if got.get(idx) else "No candles returned; tell Claude what you see.")


def replay_day(cfg: dict, client, contracts, day: date, log=print):
    """Replay one past day. Returns (trades, note)."""
    from bot.instruments import nearest_expiry, pick
    from bot.replay import day_window_ms, simulate_day, strikes_needed
    from bot.strategy import IST
    if day.weekday() >= 5:
        return [], "weekend"
    start, end = day_window_ms(day)
    idx = cfg["api"]["index_scrip"]
    index_c = client.candles([idx], start, end).get(idx, [])
    today_c = [c for c in index_c if datetime.fromtimestamp(c["ts"], IST).date() == day]
    if not today_c:
        return [], "no market data (holiday?)"
    # The contract the bot would have traded is the nearest expiry on or after that day.
    # Only contracts that haven't expired yet are in the instrument list.
    expiry = nearest_expiry(contracts, day)
    if expiry is None:
        return [], "no contracts"
    if (expiry - day).days >= 7:  # weeklies are ~7 days apart, so an older (expired) weekly was nearest
        return [], "skipped: that day's weekly contract has expired (no data for it yet)"
    if cfg["trade"]["skip_expiry_day"] and day == expiry:
        return [], "expiry day, no trading"
    needed = [c for k in strikes_needed(today_c, cfg["trade"]["strike_step"])
              for t in ("CE", "PE") if (c := pick(contracts, expiry, k, t))]
    opt_c = client.candles([c.scrip for c in needed], start, end)
    trades = simulate_day(cfg, day, index_c, opt_c, contracts, expiry, log=log)
    return trades, "traded" if trades else "no clear signal"


def _client_and_contracts(cfg: dict):
    from bot.indstocks import IndstocksClient
    from bot.instruments import load_nifty_options
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    return client, load_nifty_options(client.fno_instruments_csv(), cfg["trade"]["underlying"])


def _print_trade(t) -> None:
    print(f"  {t.opt_type} {t.strike:.0f} x{t.qty}: in {t.entry_time} @ {t.entry_price}, "
          f"out {t.exit_time} @ {t.exit_price} ({t.exit_reason}), net ₹{t.net_pnl:,.2f}")


def cmd_replay(cfg: dict, day: date) -> None:
    client, contracts = _client_and_contracts(cfg)
    trades, note = replay_day(cfg, client, contracts, day)
    print(f"{day}: {note}")
    for t in trades:
        _print_trade(t)


def cmd_backtest(cfg: dict, first: date, last: date) -> None:
    import csv
    from dataclasses import asdict
    from datetime import timedelta
    client, contracts = _client_and_contracts(cfg)
    rows, total, day = [], 0.0, first
    print(f"Backtest {first} to {last} (same rules as live, paper fills with slippage and charges)")
    print("-" * 70)
    while day <= last:
        if day.weekday() < 5:
            trades, note = replay_day(cfg, client, contracts, day, log=lambda m: None)
            print(f"{day} {day:%a}: {note}")
            for t in trades:
                _print_trade(t)
                total += t.net_pnl
                rows.append(asdict(t))
        day += timedelta(days=1)
    print("-" * 70)
    wins = sum(1 for r in rows if r["net_pnl"] > 0)
    print(f"Trades: {len(rows)}  Wins: {wins}  Losses: {len(rows) - wins}  "
          f"Net P&L: ₹{total:,.2f} on ₹{cfg['capital']['max_invested']:,} deployed")
    if rows:
        out = ROOT / "backtest.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"Saved to {out}")


def cmd_demo(cfg: dict) -> None:
    from tests.fake_market import make_day
    from bot.replay import simulate_day
    day, index_c, opt_c, contracts, expiry = make_day(seed=7)
    print(f"Made-up market day {day} (not real prices):")
    simulate_day(cfg, day, index_c, opt_c, contracts, expiry)


def main(argv: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    load_env()
    cfg = load_config()
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "check":
        cmd_check(cfg)
    elif cmd == "paper":
        from bot.live import run_live
        run_live(cfg)
    elif cmd == "replay" and len(argv) > 2:
        cmd_replay(cfg, date.fromisoformat(argv[2]))
    elif cmd == "backtest" and len(argv) > 3:
        cmd_backtest(cfg, date.fromisoformat(argv[2]), date.fromisoformat(argv[3]))
    elif cmd == "demo":
        cmd_demo(cfg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
