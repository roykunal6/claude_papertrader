# Nifty options paper-trading bot (INDmoney / INDstocks)

Paper trades Nifty options on your rules, using live INDstocks prices.
**It never places a real order.** There's no order code in it at all, only price reads.

## What it does each day

1. Before 09:15 you start it. It checks today isn't a Nifty expiry day (no trading on expiry days).
2. From 09:45 (30 minutes after the open), every minute it looks at:
   - the Nifty 50 1-min chart
   - the nearest in-the-money Call (strike just below Nifty) and Put (strike just above), nearest weekly expiry
3. **Entry:** the moment an option's 1-min trend line flips to **blue** (BUY) and that candle closes above the line,
   *and* Nifty's own line agrees (blue for a Call, red for a Put) and has held that colour for 5+ candles.
4. **Size:** as many lots as fit inside ₹30,000 (including charges).
5. **Exit:** whichever comes first:
   - +15% on the option price (target)
   - −7% stop loss
   - the line turns red and a candle closes below it (cheat-sheet rule)
   - 15:15 square-off
6. One trade a day. No clear signal means no trade.

Every number above is in `config.toml`. Change it there.

### About the trend line
TrendIkaterr is INDstocks' own indicator and its formula isn't published. This bot uses a look-alike:
an 11-period moving average that turns blue when price is above it and it's rising, and red when price is
below it and it's falling. For the first week, compare its BUY/SELL flips with your INDstocks chart.
If they don't match well, change `method` (try `"hma"`) or `length` in `config.toml`.

## One-time setup (about 15 minutes)

1. **Install Python 3.11 or newer** from python.org (on Windows, tick "Add Python to PATH").
2. Open a terminal in this folder and run:
   ```
   pip install -r requirements.txt
   ```
3. **Turn on API access** on the INDstocks website: go to *API Trading → Access tokens*.
4. Copy `.env.example` to a new file named `.env` in this folder, and paste your access token after
   `INDSTOCKS_ACCESS_TOKEN=`. The token lasts 24 hours, so you update it each morning.
   (Optional: fill in the TOTP lines instead and the bot makes its own token each day.)
   **Keep `.env` private. Never share it or paste it into chat.**
5. Test it:
   ```
   python run.py check
   ```
   You should see "Token works", the Nifty contracts, the Nifty price and some candles.

## Daily use

```
python run.py paper
```
Start it before 09:45 and leave the window open until about 15:17. It prints and saves every decision.

- `logs/YYYY-MM-DD.log` has everything it saw and did that day
- `trades.csv` has one row per paper trade with entry, exit, reason and P&L after charges

Other commands (replay and backtest only cover days whose weekly option contract hasn't expired yet,
because expired contracts aren't in INDstocks' instrument list):
```
python run.py replay 2026-11-19                # replay one past day, printing every decision
python run.py backtest 2026-11-16 2026-11-20   # replay a range of days, with a summary and backtest.csv
python run.py demo                # made-up day, no internet or token needed
python -m pytest -q               # run the rule tests
```

## Paper fills and charges
Buys are assumed 0.3% above the last price and sells 0.3% below (slippage), rounded to ₹0.05.
Charges per order: ₹10 brokerage, exchange fees, SEBI fee, STT on sells, stamp duty on buys, and GST.
Real fills on fast-moving options can be worse, so treat paper profits as a best case.
