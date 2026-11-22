"""Nifty options paper-trading bot.

  python run.py check                 test your API token and data access
  python run.py paper                 run today's live paper trading (start before 09:15)
  python run.py live                  REAL MONEY: same rules, places real orders (needs [live] enabled = true)
  python run.py live-check            read-only: your public IP, funds and open positions, before going live
  python run.py replay 2026-11-19     replay a past day with the same rules
  python run.py backtest 2026-11-16 2026-11-20   replay every day in a date range, with a summary
  python run.py export 2026-11-20     save that day's Nifty 1-min candles to a CSV
  python run.py export 2026-11-20 25400 PE   same, for one option
  python run.py test-alert            send a test alert (Mac pop-up, voice, Telegram)
  python run.py debug                 print raw API answers to help fix data problems
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
    from bot.instruments import itm_strikes, nearest_expiry, pick
    from bot.market import load_contracts
    from bot.strategy import IST
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    client.profile()
    print("1. Token works.")
    contracts = load_contracts(client, cfg, log=lambda m: print("   " + m))
    today = datetime.now(IST).date()
    exp = nearest_expiry(contracts, today)
    print(f"2. Found {len(contracts)} Nifty option contracts. Nearest expiry: {exp}. "
          f"Lot size: {contracts[0].lot_size if contracts else '?'}")
    idx = cfg["api"]["index_scrip"]
    spot = client.ltp([idx]).get(idx)
    print(f"3. Nifty index ({idx}) last price: {spot}" + ("" if spot else f"  errors: {client.last_errors}"))
    end = int(datetime.now(IST).timestamp() * 1000)
    got = client.candles([idx], end - 4 * 86400 * 1000, end)
    n = len(got.get(idx, []))
    print(f"4. Got {n} one-minute Nifty candles for the last 4 days." + ("" if n else f"  errors: {client.last_errors}"))
    if spot and exp:
        ce_k, _ = itm_strikes(spot, cfg["trade"]["strike_step"])
        c = pick(contracts, exp, ce_k, "CE")
        if c:
            oc = client.candles([c.scrip], end - 4 * 86400 * 1000, end).get(c.scrip, [])
            ltp = client.ltp([c.scrip]).get(c.scrip)
            print(f"5. Option {c.symbol} ({c.scrip}): last price {ltp}, {len(oc)} one-minute candles."
                  + ("" if oc else f"  errors: {client.last_errors}"))
    print("All good." if n and contracts else "Something is missing above; send this to Claude.")


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
    from bot.market import load_contracts
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    return client, load_contracts(client, cfg)


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


def cmd_debug(cfg: dict) -> None:
    """Print raw API answers (no token, no personal data) so Claude can match the real formats."""
    import csv
    import io
    from bot.indstocks import IndstocksClient
    from bot.strategy import IST
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    s = client.session

    def show(label, path, params=None, n=700):
        print(f"\n=== {label}: GET {path} {params or ''}")
        try:
            r = s.get(client.base_url + path, params=params, timeout=20)
        except Exception as e:  # noqa: BLE001
            print(f"request failed: {e.__class__.__name__}: {str(e)[:200]}")
            return None
        print(f"HTTP {r.status_code}, content-type {r.headers.get('content-type')}, {len(r.content)} bytes")
        print(r.text[:n])
        return r

    for source in ("fno", "index"):
        try:
            r = s.get(client.base_url + "/market/instruments", params={"source": source}, timeout=60)
        except Exception as e:  # noqa: BLE001
            print(f"\n=== instruments source={source}: request failed: {e.__class__.__name__}: {str(e)[:200]}")
            continue
        print(f"\n=== instruments source={source}: HTTP {r.status_code}, "
              f"content-type {r.headers.get('content-type')}, {len(r.content)} bytes")
        text = r.text
        lines = text.splitlines()
        print("first 3 lines:")
        for ln in lines[:3]:
            print("  " + ln[:400])
        hits = [ln for ln in lines if "NIFTY" in ln.upper()
                and "BANK" not in ln.upper() and "FIN" not in ln.upper() and "MID" not in ln.upper()]
        print(f"{len(hits)} lines mention NIFTY; first 5:")
        for ln in hits[:5]:
            print("  " + ln[:400])
        if source == "fno" and lines:
            rows = list(csv.DictReader(io.StringIO(text)))
            if rows:
                print("column names:", list(rows[0].keys()))

    end = int(datetime.now(IST).timestamp() * 1000)
    start = end - 4 * 86400 * 1000
    for code in (cfg["api"]["index_scrip"], "NIDX_40000001", "IDX_40000001", "INDEX_40000001", "NSE_26000"):
        show(f"LTP {code}", "/market/quotes/ltp", {"scrip-codes": code}, 300)
    show("candles index", "/market/historical/1minute",
         {"scrip-codes": cfg["api"]["index_scrip"], "start_time": start, "end_time": end}, 500)
    show("candles RELIANCE (equity, as a control)", "/market/historical/1minute",
         {"scrip-codes": "NSE_2885", "start_time": start, "end_time": end}, 500)
    show("LTP RELIANCE (control)", "/market/quotes/ltp", {"scrip-codes": "NSE_2885"}, 300)
    print("\nDone. Copy everything above and send it to Claude. It contains no token or account details.")


def cmd_export(cfg: dict, day: date, option: list | None = None) -> None:
    """Save that day's Nifty 1-min candles to a CSV, to compare against the INDstocks chart."""
    import csv
    from bot.indstocks import IndstocksClient
    from bot.replay import day_window_ms
    from bot.strategy import IST
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    start, end = day_window_ms(day, warmup_days=4)
    code, name = cfg["api"]["index_scrip"], "nifty"
    if option:
        from bot.instruments import nearest_expiry, pick
        from bot.market import load_contracts
        strike, opt_type = float(option[0]), option[1].upper()
        contracts = load_contracts(client, cfg)
        c = pick(contracts, nearest_expiry(contracts, day), strike, opt_type)
        if not c:
            print(f"No live contract for {strike:.0f} {opt_type} on the expiry after {day}.")
            return
        code, name = c.scrip, f"nifty_{strike:.0f}{opt_type}"
    rows = client.candles([code], start, end).get(code, [])
    out = ROOT / f"{name}_{day:%Y-%m-%d}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_ist", "open", "high", "low", "close", "volume"])
        for c in rows:
            w.writerow([f"{datetime.fromtimestamp(c['ts'], IST):%Y-%m-%d %H:%M}", c["o"], c["h"], c["l"], c["c"], c["v"]])
    print(f"Saved {len(rows)} candles (that day plus 4 days before, for warm-up) to {out}")


LIVE_PHRASE = "REAL MONEY"


def cmd_live(cfg: dict) -> None:
    if not cfg.get("live", {}).get("enabled", False):
        print("Live trading is OFF. To turn it on, set  enabled = true  under [live] in config.toml.\n"
              "Until then, use:  python run.py paper")
        return
    print("=" * 70)
    print("  LIVE MODE: this bot will place REAL orders in your INDmoney account.")
    print(f"  Up to ₹{cfg['capital']['max_invested']:,} on one Nifty option trade today.")
    print("  To stop at any time: create a file named STOP in this folder (it sells and exits),")
    print("  or press Ctrl+C and manage the position yourself in the INDmoney app.")
    print("=" * 70)
    typed = input(f'Type  {LIVE_PHRASE}  to start live trading (anything else = cancel): ').strip()
    if typed != LIVE_PHRASE:
        print("Cancelled. Nothing was done.")
        return
    from bot.live import run_live
    run_live(cfg, live=True)


def cmd_live_check(cfg: dict) -> None:
    """Read-only. Nothing is bought or sold."""
    import requests
    from bot.broker import LiveBroker
    from bot.indstocks import IndstocksClient
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception as e:  # noqa: BLE001
        ip = f"(couldn't check: {e.__class__.__name__})"
    print(f"1. Your public IP right now: {ip}")
    print("   INDstocks only accepts orders from the IP you registered under API Trading > Access tokens.")
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    client.profile()
    print("2. Token works.")
    b = LiveBroker(client, cfg)
    try:
        f = b.option_buy_funds()
        print(f"3. Funds for option buying: ₹{f:,.0f}" if f is not None else "3. Funds: couldn't read option_buy balance.")
    except Exception as e:  # noqa: BLE001
        print(f"3. Funds check failed: {e}")
    try:
        held = b.open_nifty_positions()
        print(f"4. Open Nifty positions: {len(held)}" + "".join(
            f"\n   {p.get('symbol')} qty {p.get('net_qty')} avg ₹{p.get('avg_price')}" for p in held))
    except Exception as e:  # noqa: BLE001
        print(f"4. Positions check failed: {e}")
    try:
        print(f"5. Orders placed by this bot today: {len(b.bot_orders_today())}")
    except Exception as e:  # noqa: BLE001
        print(f"5. Order book check failed: {e}")
    print("Live trading is", "ON" if cfg.get("live", {}).get("enabled") else "OFF", "in config.toml.")


def cmd_test_alert(cfg: dict) -> None:
    from bot.alerts import Alerter
    a = Alerter(cfg)
    a.send("🟢 TEST ALERT", "If you can see and hear this, alerts work. Nothing was bought.", speak="This is a test alert.")
    print("Mac pop-up:", "on" if a.mac else "off (not a Mac or turned off)",
          "| Telegram:", "on" if (a.telegram_token and a.telegram_chat) else "off (no TELEGRAM_* in .env)")


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
        try:
            cmd_check(cfg)
        except Exception as e:  # noqa: BLE001
            print(f"\nCheck stopped: {e.__class__.__name__}: {e}\nSend this to Claude.")
    elif cmd == "paper":
        from bot.live import run_live
        run_live(cfg)
    elif cmd == "replay" and len(argv) > 2:
        cmd_replay(cfg, date.fromisoformat(argv[2]))
    elif cmd == "backtest" and len(argv) > 3:
        cmd_backtest(cfg, date.fromisoformat(argv[2]), date.fromisoformat(argv[3]))
    elif cmd == "debug":
        cmd_debug(cfg)
    elif cmd == "export" and len(argv) > 2:
        cmd_export(cfg, date.fromisoformat(argv[2]), argv[3:5] if len(argv) > 4 else None)
    elif cmd == "live":
        cmd_live(cfg)
    elif cmd == "live-check":
        try:
            cmd_live_check(cfg)
        except Exception as e:  # noqa: BLE001
            print(f"\nCheck stopped: {e.__class__.__name__}: {e}\nSend this to Claude.")
    elif cmd in ("test-alert", "alert"):
        cmd_test_alert(cfg)
    elif cmd == "demo":
        cmd_demo(cfg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
