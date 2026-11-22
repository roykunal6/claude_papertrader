# Nifty options paper-trading bot (INDmoney / INDstocks)

Trades Nifty options on your rules, using live INDstocks prices.
- `python run.py paper` (the default) **never places a real order**. It tells you what to do and keeps score.
- `python run.py live` places **real orders** by itself. It's off until you switch it on (see "Going live").

## The super-simple version

1. Each morning before 9:15, open Terminal in this folder and type `python run.py paper`. Leave it open.
2. At 9:45 it tells you **today's strike**: one strike number, the one nearest Nifty, used for both the Call and the Put all day.
3. When the line turns blue, your Mac **pops up, dings and says "Buy now"**, and your phone gets a Telegram
   message saying exactly what to buy, how many, the price, and the stop.
4. If the price keeps going up, it **moves your stop up** and tells you the new number.
5. When it's time to get out, it says **"Sell now"** and why. That's the day done.

You never have to read the chart yourself. Do only what the alert says.

## What it does each day

1. Before 09:15 you start it. It checks today isn't a Nifty expiry day (no trading on expiry days).
2. From 09:45 (30 minutes after the open), every minute it looks at:
   - the Nifty 50 1-min chart
   - one strike, the one nearest Nifty at 09:45, nearest weekly expiry. Its Call and its Put are watched all day
     (`strike_mode = "single"`; `"lock"` keeps separate in-the-money Call and Put strikes instead).
3. **Entry:** the moment an option's 1-min trend line flips to **blue** (BUY) and that candle closes above the line,
   *and* Nifty's own line agrees (blue for a Call, red for a Put) and has held that colour for 5+ candles.
4. **Size:** as many lots as fit inside ₹30,000 (including charges).
5. **Exit:** whichever comes first:
   - the stop loss, which starts 7% below your buy price and only ever moves up (the trailing stop, below)
   - +15% target, only if you set `target_mode = "exit"`. By default it keeps riding past +15% on a tighter stop
   - the line turns red and a candle closes below it (cheat-sheet rule)
   - 15:15 square-off
6. One trade a day. No clear signal means no trade.

Every number above is in `config.toml`. Change it there.

### The trailing stop, in plain words
- At the start, the stop is **7% below** what you paid.
- Once the option is **up 5%**, the stop moves to **what you paid**. From then on you can't lose on the trade (apart from charges).
- After that the stop follows **7% below the highest price so far**.
- Once it's **up 15%**, it doesn't sell. It tightens to **4% below the highest price**, so it keeps more profit if the move continues.
- The stop never moves down. All these numbers are in the `[trail]` part of `config.toml`.

### About the trend line
TrendIkaterr is INDstocks' own indicator and its formula isn't published. This bot uses a look-alike:
an 11-period moving average that turns blue when price is above it and it's rising, and red when price is
below it and it's falling. For the first week, compare its BUY/SELL flips with your INDstocks chart.
If they don't match well, change `method` (try `"hma"`) or `length` in `config.toml`.

## One-time setup (about 15 minutes)

1. **Python 3.9 or newer.** A Mac's built-in `python3` works. On Windows, install from python.org and tick "Add Python to PATH".
2. Open a terminal in this folder and run:
   ```
   python3 -m pip install -r requirements.txt
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

## Alerts on your phone (Telegram, optional, 5 minutes)

Mac pop-ups and the voice work with no setup. To get alerts on your phone too:
1. In Telegram, open **@BotFather**, send `/newbot`, and follow the steps. It gives you a **token**.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser
   and find `"chat":{"id": ...}`. That number is your **chat ID**.
3. Add two lines to your `.env` file (never paste them into chat):
   ```
   TELEGRAM_BOT_TOKEN=your-token
   TELEGRAM_CHAT_ID=your-chat-id
   ```
4. Run `python run.py test-alert`. You should see a pop-up, hear a voice, and get a Telegram message.

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
python run.py live-check                       # read-only check before going live (IP, funds, positions)
python run.py test-alert                       # check the pop-up, voice and Telegram
python run.py export 2026-11-20                # Nifty 1-min candles to a CSV
python run.py export 2026-11-20 25400 PE       # one option's 1-min candles to a CSV
python run.py demo                # made-up day, no internet or token needed
python -m pytest -q               # run the rule tests
```

## Going live (real money)

Do this only after paper trading has looked right for a while. You can go back to paper at any time.

1. **Static IP.** INDstocks only accepts orders from an IP address you register (exchange rule).
   Run `python run.py live-check`. It shows your current public IP, your funds and any open Nifty positions,
   and buys nothing. Register that IP at INDstocks *API Trading → Access tokens*. Most home internet IPs change
   now and then. If yours changes, orders get rejected and the bot tells you. Your internet provider can sell you
   a fixed IP.
2. In `config.toml`, under `[live]`, change `enabled = false` to `enabled = true`.
3. Each trading morning run `python run.py live` and type `REAL MONEY` when asked.

What it does in live mode:
- Same rules, same alerts, but it **buys and sells by itself**, then tells you "BOUGHT" or "SOLD".
- One trade a day, at most ₹30,000. Before starting it checks your account: if you already hold a Nifty option
  position, or the bot already bought today, it won't take a new trade.
- Orders are limit orders priced 1% through the last price so they fill at once. Unfilled buys are cancelled.
  Sells are re-priced and retried. If a sell still can't complete, you get a loud **SELL BY HAND NOW** alert.
- **Emergency stop:** create an empty file named `STOP` in this folder (`touch STOP`). The bot sells and exits.
  Delete the file before the next day.
- Keep the Mac awake and online until 15:17 (System Settings → Battery/Energy → prevent sleep). If the Mac or
  internet dies with a trade open, the bot can't sell it, so check the INDmoney app.
  Orders are INTRADAY, so INDmoney squares off anything left open near the close.

## Paper fills and charges
Buys are assumed 0.3% above the last price and sells 0.3% below (slippage), rounded to ₹0.05.
Charges per order: ₹10 brokerage, exchange fees, SEBI fee, STT on sells, stamp duty on buys, and GST.
Real fills on fast-moving options can be worse, so treat paper profits as a best case.
