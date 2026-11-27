"""Nifty options paper-trading bot.

  python run.py check                 test your API token and data access
  python run.py paper                 run today's live paper trading (start before 09:15)
  python run.py watch                 alerts + paper trades from the REAL TrendIkaterr on your chart
                                      (needs the Chrome extension in chart_watcher/, see README)
  python run.py live                  REAL MONEY: same rules, places real orders (needs [live] enabled = true)
  python run.py live-check            read-only: your public IP, funds and open positions, before going live
  python run.py replay 2026-11-19     replay a past day with the same rules
  python run.py backtest 2026-11-09 2026-11-20   replay every day in a date range, with a summary
                                      (expired weekly contracts are fetched too)
  python run.py backtest 12months     the same, for the last 12 months (or 3months, 6months...)
  python run.py compare 12months      test every strategy version on the same days, with an honesty check
  python run.py export 2026-11-20     save that day's Nifty 1-min candles to a CSV
  python run.py export 2026-11-20 25400 PE   same, for one option
  python run.py test-alert            send a test alert (Mac pop-up, voice, Telegram)
  python run.py debug                 print raw API answers to help fix data problems
  python run.py debug-expired 2026-11-12   raw answers of the expired-contract calls for one past day
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


_EXPIRED: dict = {}  # cache for one run: expiry list and chains of expired weeklies


def _expired_chain(cfg: dict, client, day: date):
    """(expiry, contracts) for the weekly that was current on `day`, if it has expired since."""
    from datetime import timedelta
    from bot.indstocks import expired_contracts, expired_expiries
    und = cfg["trade"]["underlying"]
    want_from = day - timedelta(days=7)
    if "from" not in _EXPIRED or want_from < _EXPIRED["from"]:
        upto = _EXPIRED.get("from", date.today() + timedelta(days=1)) - timedelta(days=1)
        got = expired_expiries(client, und, want_from, upto)
        _EXPIRED["expiries"] = sorted(set(_EXPIRED.get("expiries", [])) | set(got))
        _EXPIRED["from"] = want_from
        if not _EXPIRED["expiries"]:
            raise RuntimeError("the expired-expiries API returned no Nifty expiries; run `python run.py "
                               f"debug-expired {day}` and send the output to Claude")
    exp = next((e for e in _EXPIRED["expiries"] if e >= day), None)
    if exp is None or (exp - day).days > 10:
        return None, []
    if exp not in _EXPIRED:
        _EXPIRED[exp] = expired_contracts(client, und, exp)
    return exp, _EXPIRED[exp]


def _chain_for(cfg: dict, client, contracts, d: date):
    """(expiry, chain, expired) for the nearest weekly expiry on or after d. Raises on API trouble."""
    from bot.instruments import nearest_expiry
    expiry = nearest_expiry(contracts, d)
    if expiry is not None and (expiry - d).days < 7:
        return expiry, contracts, False
    old_exp, old_chain = _expired_chain(cfg, client, d)  # that week has expired: use the expired-contracts API
    if not old_chain:
        raise LookupError(f"no expired contract list for that week (run: python run.py debug-expired {d})")
    return old_exp, old_chain, True


def load_day(cfg: dict, client, contracts, day: date, cache_dir: Path | None = None) -> dict:
    """Download everything one past day needs: Nifty candles (with 4 days before, to warm up the averages),
    and 1-min candles of every in-the-money strike Nifty touched, for that day's weekly expiry
    (and next week's too on an expiry day). Saved to cache_dir so later runs don't download again."""
    import gzip
    import json
    from dataclasses import asdict
    from datetime import timedelta
    from bot.indstocks import expired_candles
    from bot.instruments import pick
    from bot.replay import day_window_ms, strikes_needed
    from bot.strategy import IST
    path = cache_dir / f"{day}.json.gz" if cache_dir else None
    if path and path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    start, end = day_window_ms(day)
    day_start, _ = day_window_ms(day, warmup_days=0)
    idx = cfg["api"]["index_scrip"]
    index_c = client.candles([idx], start, end).get(idx, [])
    today_c = [c for c in index_c if datetime.fromtimestamp(c["ts"], IST).date() == day]
    data = {"day": day.isoformat(), "index": index_c, "chains": {}, "note": None}
    if not today_c:
        data["note"] = "no market data (holiday?)"
    else:
        ks = strikes_needed(today_c, cfg["trade"]["strike_step"])
        expiry, chain, expired = _chain_for(cfg, client, contracts, day)
        todo = [(expiry, chain, expired)]
        if expiry == day:
            todo.append(_chain_for(cfg, client, contracts, day + timedelta(days=1)))
        for exp, ch, was_expired in todo:
            needed = [c for k in ks for t in ("CE", "PE") if (c := pick(ch, exp, k, t))]
            scrips = [c.scrip for c in needed]
            got = expired_candles(client, scrips, day_start, end) if was_expired else client.candles(scrips, day_start, end)
            data["chains"][exp.isoformat()] = {
                "expired": was_expired,
                "contracts": [dict(asdict(c), expiry=c.expiry.isoformat()) for c in needed],
                "candles": {k: v for k, v in got.items() if v}}
        if not any(ch["candles"] for ch in data["chains"].values()):
            raise LookupError("no option candles came back for that day"
                              + (f" (run: python run.py debug-expired {day})" if expired else ""))
    if path and (data["note"] or day < date.today()):
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f)
    return data


def run_day(cfg: dict, data: dict, day: date, log=print):
    """Replay one day from load_day's data with the rules in cfg. Returns (trades, note)."""
    from bot.instruments import Contract
    from bot.replay import simulate_day
    if data.get("note"):
        return [], data["note"]
    exps = sorted(date.fromisoformat(e) for e in data["chains"])
    expiry = exps[0]
    if expiry == day:
        if cfg["trade"]["skip_expiry_day"]:
            return [], "expiry day, no trading"
        if cfg["trade"].get("expiry_day_contract", "next") == "next":
            if len(exps) < 2:
                return [], "skipped: no data for next week's contract"
            expiry = exps[1]
    ch = data["chains"][expiry.isoformat()]
    chain = [Contract(**dict(c, expiry=date.fromisoformat(c["expiry"]))) for c in ch["contracts"]]
    trades = simulate_day(cfg, day, data["index"], ch["candles"], chain, expiry, log=log)
    return trades, "traded" if trades else "no trade (no clean signal)"


def replay_day(cfg: dict, client, contracts, day: date, log=print, cache_dir: Path | None = None):
    """Replay one past day. Returns (trades, note). Works for expired weeklies too."""
    if day.weekday() >= 5:
        return [], "weekend"
    try:
        data = load_day(cfg, client, contracts, day, cache_dir)
    except LookupError as e:
        return [], f"skipped: {e}"
    except Exception as e:  # noqa: BLE001
        return [], f"skipped: couldn't load that day's data ({e.__class__.__name__}: {str(e)[:200]})"
    return run_day(cfg, data, day, log)


CACHE = ROOT / "data" / "cache"


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


def summarize(rows: list[dict], notes: dict[str, int], cfg: dict) -> list[str]:
    """Plain-English summary of a backtest: totals, win rate, worst run of losses, monthly P&L."""
    out = []
    total = sum(r["net_pnl"] for r in rows)
    wins = [r["net_pnl"] for r in rows if r["net_pnl"] > 0]
    losses = [r["net_pnl"] for r in rows if r["net_pnl"] <= 0]
    days = sum(notes.values())
    out.append(f"Weekdays checked: {days}. " + ", ".join(f"{k}: {v}" for k, v in sorted(notes.items(), key=lambda x: -x[1])))
    out.append(f"Trades: {len(rows)}  Wins: {len(wins)}  Losses: {len(losses)}  "
               f"Win rate: {100 * len(wins) / len(rows):.0f}%" if rows else "Trades: 0")
    if not rows:
        return out
    out.append(f"Net P&L after charges: ₹{total:,.0f}   Average per trade: ₹{total / len(rows):,.0f}   "
               f"Average per trading day checked: ₹{total / max(1, days):,.0f}")
    if wins and losses:
        out.append(f"Average win: ₹{sum(wins) / len(wins):,.0f}   Average loss: ₹{sum(losses) / len(losses):,.0f}")
    peak = run = dd = 0.0
    streak = worst_streak = 0
    for r in rows:
        run += r["net_pnl"]
        peak = max(peak, run)
        dd = min(dd, run - peak)
        streak = streak + 1 if r["net_pnl"] <= 0 else 0
        worst_streak = max(worst_streak, streak)
    best = max(rows, key=lambda r: r["net_pnl"])
    worst = min(rows, key=lambda r: r["net_pnl"])
    out.append(f"Best trade: {best['date']} ₹{best['net_pnl']:,.0f}   Worst trade: {worst['date']} ₹{worst['net_pnl']:,.0f}")
    out.append(f"Biggest drop from a high point (max drawdown): ₹{dd:,.0f}   Longest losing streak: {worst_streak} trades")
    out.append(f"Return on ₹{cfg['capital']['total']:,} capital: {100 * total / cfg['capital']['total']:.1f}%")
    months: dict[str, float] = {}
    for r in rows:
        months[r["date"][:7]] = months.get(r["date"][:7], 0.0) + r["net_pnl"]
    out.append("By month: " + "  ".join(f"{m}: ₹{v:,.0f}" for m, v in sorted(months.items())))
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[r["exit_reason"]] = reasons.get(r["exit_reason"], 0) + 1
    out.append("Exits: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    return out


def cmd_backtest(cfg: dict, first: date, last: date) -> None:
    import csv
    from dataclasses import asdict
    from datetime import timedelta
    client, contracts = _client_and_contracts(cfg)
    rows, notes, day = [], {}, first
    print(f"Backtest {first} to {last}: strategy \"{cfg.get('strategy', {}).get('name', 'orb')}\", same rules as live, "
          "paper fills with slippage and charges")
    print("-" * 70)
    while day <= last:
        if day.weekday() < 5:
            try:
                trades, note = replay_day(cfg, client, contracts, day, log=lambda m: None, cache_dir=CACHE)
            except Exception as e:  # noqa: BLE001  keep going: one bad day shouldn't stop a long backtest
                trades, note = [], f"skipped: error {e.__class__.__name__}: {str(e)[:150]}"
            print(f"{day} {day:%a}: {note}", flush=True)
            key = note.split(" (")[0].split(":")[0] if note.startswith(("skipped", "no market")) else note
            notes[key] = notes.get(key, 0) + 1
            for t in trades:
                _print_trade(t)
                rows.append(asdict(t))
        day += timedelta(days=1)
    print("-" * 70)
    for line in summarize(rows, notes, cfg):
        print(line)
    if rows:
        out = ROOT / "backtest.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"Every trade saved to {out}")


def cmd_compare(cfg: dict, first: date, last: date) -> None:
    """Download each day once (cached in data/cache), then test every strategy version on the same days."""
    import csv
    from datetime import timedelta
    from bot import compare
    client, contracts = _client_and_contracts(cfg)
    days, skipped, day = [], {}, first
    print(f"Loading {first} to {last} (first run downloads; later runs read data/cache and are fast)...")
    while day <= last:
        if day.weekday() < 5:
            try:
                data = load_day(cfg, client, contracts, day, CACHE)
                if data.get("note"):
                    skipped[data["note"]] = skipped.get(data["note"], 0) + 1
                else:
                    days.append((day, data))
            except Exception as e:  # noqa: BLE001
                key = f"skipped ({e.__class__.__name__})"
                skipped[key] = skipped.get(key, 0) + 1
                print(f"  {day}: {str(e)[:150]}")
        day += timedelta(days=1)
    print(f"Trading days with data: {len(days)}. Not used: "
          + (", ".join(f"{k}: {v}" for k, v in skipped.items()) or "none") + "\n")
    if not days:
        return
    rows = compare.compare(cfg, days, run_day)
    out = ROOT / "compare.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved to {out}. Send Claude the table above (or this file).")


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
    from datetime import timedelta
    d0 = datetime.now(IST).date()
    show("expired expiries", "/market/instruments/expired/expiries",
         {"underlying": "NIFTY", "segment": "DERIVATIVE",
          "expiry_from": (d0 - timedelta(days=30)).isoformat(), "expiry_to": d0.isoformat()}, 500)
    print("\nDone. Copy everything above and send it to Claude. It contains no token or account details.")


def cmd_debug_expired(cfg: dict, day: date) -> None:
    """Print the raw answers of the three expired-contract calls for one past day (no token or account data)."""
    import json
    from datetime import timedelta
    from bot.indstocks import IndstocksClient, expired_candles, expired_contracts, expired_expiries
    from bot.replay import day_window_ms
    client = IndstocksClient.from_env(cfg["api"]["base_url"])
    und = cfg["trade"]["underlying"]

    def raw(path, params):
        print(f"\n=== GET {path} {params}")
        try:
            body = client._get(path, params)
            print(json.dumps(body)[:1500])
            return body
        except Exception as e:  # noqa: BLE001
            print(f"failed: {e.__class__.__name__}: {str(e)[:300]}")
            return None

    raw("/market/instruments/expired/expiries", {"underlying": und, "segment": "DERIVATIVE",
        "expiry_from": (day - timedelta(days=7)).isoformat(), "expiry_to": (day + timedelta(days=10)).isoformat()})
    try:
        exps = expired_expiries(client, und, day - timedelta(days=7), day + timedelta(days=10))
    except Exception as e:  # noqa: BLE001
        print(f"parse failed: {e}")
        exps = []
    print(f"\nParsed expiries: {exps}")
    exp = next((e for e in exps if e >= day), None)
    if not exp:
        print("No expiry found on or after that day. Send this output to Claude.")
        return
    raw("/market/instruments/expired/contracts", {"underlying": und, "segment": "DERIVATIVE", "expiry": exp.isoformat()})
    chain = expired_contracts(client, und, exp)
    print(f"\nParsed {len(chain)} option contracts for expiry {exp}. First 3: {chain[:3]}")
    if not chain:
        return
    start, end = day_window_ms(day, warmup_days=0)
    sample = sorted(chain, key=lambda c: c.strike)[len(chain) // 2: len(chain) // 2 + 2]
    raw("/market/historical/expired/1minute", {"trading-symbols": ",".join(c.symbol for c in sample),
        "start_time": start, "end_time": end, "segment": "NFO"})
    got = expired_candles(client, [c.scrip for c in sample], start, end)
    print("\nParsed candles: " + ", ".join(f"{k}: {len(v)}" for k, v in got.items()))
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
    from tests.fake_market import make_day, orb_path
    from bot.replay import simulate_day
    day, index_c, opt_c, contracts, expiry = make_day(path=orb_path(up=True))
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
    elif cmd in ("backtest", "compare") and len(argv) > 2 and argv[2].endswith("months"):
        from datetime import timedelta
        months = int(argv[2].replace("months", ""))
        end = date.today() - timedelta(days=1)
        (cmd_backtest if cmd == "backtest" else cmd_compare)(cfg, end - timedelta(days=round(months * 30.4)), end)
    elif cmd == "compare" and len(argv) > 3:
        cmd_compare(cfg, date.fromisoformat(argv[2]), date.fromisoformat(argv[3]))
    elif cmd == "debug":
        cmd_debug(cfg)
    elif cmd == "debug-expired" and len(argv) > 2:
        cmd_debug_expired(cfg, date.fromisoformat(argv[2]))
    elif cmd == "export" and len(argv) > 2:
        cmd_export(cfg, date.fromisoformat(argv[2]), argv[3:5] if len(argv) > 4 else None)
    elif cmd == "watch":
        from bot.chart_watch import run_watch
        run_watch(cfg)
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
