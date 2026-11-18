"""Nifty options paper-trading bot.

  python run.py check                 test your API token and data access
  python run.py paper                 run today's live paper trading (start before 09:15)
  python run.py replay 2026-11-17     replay a past day with the same rules
  python run.py demo                  run on a made-up day, no API needed
"""
from __future__ import annotations

import os
import sys
import tomllib
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


def cmd_replay(cfg: dict, day: date) -> None:
    from bot.indstocks import IndstocksClient
    from bot.instruments import load_nifty_options, nearest_expiry, pick
    from bot.replay import day_window_ms, simulate_day, strikes_needed
    from bot.strategy import IST
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    contracts = load_nifty_options(client.fno_instruments_csv(), cfg["trade"]["underlying"])
    expiry = nearest_expiry(contracts, day)
    start, end = day_window_ms(day)
    idx = cfg["api"]["index_scrip"]
    index_c = client.candles([idx], start, end).get(idx, [])
    today_c = [c for c in index_c if datetime.fromtimestamp(c["ts"], IST).date() == day]
    if not expiry or not today_c:
        print("No data for that day (holiday, or its option contracts have expired). "
              "Replay works for days in the current expiry week.")
        return
    needed = [c for k in strikes_needed(today_c, cfg["trade"]["strike_step"])
              for t in ("CE", "PE") if (c := pick(contracts, expiry, k, t))]
    opt_c = client.candles([c.scrip for c in needed], start, end)
    trades = simulate_day(cfg, day, index_c, opt_c, contracts, expiry)
    for t in trades:
        print(f"  {t.opt_type} {t.strike:.0f} x{t.qty}: in {t.entry_time} @ {t.entry_price}, "
              f"out {t.exit_time} @ {t.exit_price} ({t.exit_reason}), net ₹{t.net_pnl:,.2f}")


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
    elif cmd == "demo":
        cmd_demo(cfg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
