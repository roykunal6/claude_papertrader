# Nifty options paper-trading bot (INDmoney / INDstocks)

Trades Nifty options on your rules, using live INDstocks prices.
- `python run.py paper` (the default) **never places a real order**. It tells you what to do and keeps score.
- `python run.py live` places **real orders** by itself. It's off until you switch it on (see "Going live").

## The super-simple version

1. Each morning before 9:15, open Terminal in this folder and type `python run.py paper`. Leave it open.
2. At 9:45 it tells you **today's opening range**: "I'll buy a CALL if Nifty breaks above X, a PUT if it breaks below Y."
3. If Nifty breaks out, your Mac **pops up, dings and says "Buy now"**, and your phone gets a Telegram
   message saying exactly what to buy, how many, and the price.
4. When it's time to get out, it says **"Sell now"** and why. That's the day done.
5. Many days there is **no trade**. That's normal and on purpose.

You never have to read the chart yourself. Do only what the alert says.

## The strategy: Opening Range Breakout (ORB)

A well-known, long-studied intraday method. Everything is decided on the **Nifty 50 index**, not the option
chart, because the option's 1-minute chart is noisy and was causing 1 to 7 minute whipsaw exits.

1. **09:15 to 09:45: watch only.** Note Nifty's highest and lowest price. That's the *opening range*.
2. **Skip the day** if the range is wider than 0.9% of Nifty (wild open) or narrower than 0.15% (dead market).
3. **Entry (09:45 to 13:30):** at the end of each 5-minute candle:
   - Nifty closes **above the range high** (by 5+ points) and above its 20-candle average: buy the in-the-money **CALL**
   - Nifty closes **below the range low** (by 5+ points) and below its 20-candle average: buy the in-the-money **PUT**
   - If Nifty is already more than 90 points from the stop, it doesn't chase.
4. **Size:** as many lots as keep the loss at the stop under **₹2,500**, and never more than ₹30,000 spent.
   With ₹50,000 capital that is usually **1 lot**.
5. **Exit**, whichever comes first:
   - **Stop:** a 1-minute Nifty candle closes back past the **middle of the range** (the breakout failed)
   - **Breakeven:** once Nifty has gone 1x the risk in our favour, the stop moves to the entry level
   - **Target:** Nifty moves **2x the risk** in our favour
   - **Safety net:** the option itself falls 25%
   - **15:15** square-off
6. One trade a day. Nearest weekly expiry (next week's on expiry day).

Every number above is in `config.toml` (`[timing]`, `[trade]`, `[orb]`). Change it there.
The old TrendIkaterr look-alike has been removed.

## The second strategy: Trend-following (`name = "trend"` under `[strategy]`)

Trades only when Nifty's trend is clear, and holds winners with a trailing stop.

1. **Clear trend** on the 5-minute chart means: the 9-candle average is above the 21-candle average
   (and the 21 is rising), ADX (a standard trend-strength gauge) is 22 or more, and Nifty is above today's open.
   The mirror image is a clear downtrend. Anything else: no trade.
2. **Entry:** when a clear trend starts, or when Nifty dips to its 9-candle average inside the trend and then
   moves on again. Up-trend: buy the in-the-money **CALL**. Down-trend: the **PUT**.
3. **Stop:** 1.5x the average 5-minute candle range. After 1x that risk in profit it moves to the entry level, then
   **trails** 1.5x the risk behind the best price and never moves back. It also exits if the trend ends
   (the averages cross back). There's no fixed target.
4. Up to **3 trades a day**, but it stops for the day after 2 losing trades or ₹5,000 lost.

## The third option: Afternoon momentum (`name = "late"`, or `"orb+late"` for both)

Based on a well-known study ("Market Intraday Momentum", Journal of Financial Economics, 2018): the market's
early move tends to carry on into the last half hour.
- At 14:45, if Nifty is up at least 0.2% from yesterday's close by 09:45 **and** still rising over the last
  30 minutes, buy the in-the-money **CALL**. Both down: the **PUT**. Mixed: no trade.
- Exit at 15:15, or earlier if Nifty moves 30 points against it. One such trade a day.
- `"orb+late"` runs the morning breakout and this afternoon trade on the same day (they never overlap).

## Expiry days
`skip_expiry_day = false` trades on expiry days too. On those days it uses **next week's** contract
(`expiry_day_contract = "next"`), because the expiring one can lose most of its value in minutes.

## Which strategy is better? Compare them
```
python run.py compare 12months
```
This downloads each day once (into `data/cache`, so later runs take a minute), then tests every version on
exactly the same days: the breakout as tested, the breakout with expiry days, the breakout with a trailing stop,
the trend strategy with 3 or 1 trades a day, and afternoon momentum alone and with the breakout. It also runs an
**honesty check**: it tries 27 settings each of the trend and afternoon strategies on the earlier half of the year,
picks the best, and shows how that pick did on the later half, which it never saw.
If a version only looks good on the half it was picked on, that was luck.

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

Other commands:
```
python run.py compare 12months                 # test every strategy version on the same days (see above)
python run.py backtest 12months                # replay the last 12 months with the strategy in config.toml
python run.py backtest 2026-06-01 2026-11-20   # replay a date range, with a summary and backtest.csv
python run.py replay 2026-11-19                # replay one past day, printing every decision
python run.py debug-expired 2026-11-12         # if old weeks get "skipped", run this and send Claude the output
python run.py live-check                       # read-only check before going live (IP, funds, positions)
python run.py test-alert                       # check the pop-up, voice and Telegram
python run.py export 2026-11-20                # Nifty 1-min candles to a CSV
python run.py export 2026-11-20 25400 PE       # one option's 1-min candles to a CSV
python run.py demo                # made-up day, no internet or token needed
python -m pytest -q               # run the rule tests
```

## Alerts and paper trades from the REAL TrendIkaterr on your chart

INDstocks charts have no alerts, and TrendIkaterr isn't a public TradingView script, so a TradingView
webhook can't see it. Instead, a small Chrome extension reads the indicator's colour straight off your
open INDstocks chart (blue = bullish, red = bearish, green = recovery) and tells the bot on your Mac.
Nothing is re-built: whatever TrendIkaterr shows on your screen is what the bot acts on.

**One-time setup (2 minutes)**
1. In Chrome open `chrome://extensions`, switch on **Developer mode** (top right).
2. Click **Load unpacked** and pick the `chart_watcher` folder inside this project.

**Every trading day**
1. In Terminal: `cd ~/Desktop/Github/claude_papertrader && source .venv/bin/activate && python run.py watch`
2. In Chrome open `indstocks.com/app/flash` with **TRENDIKATERR** on the Nifty 50 chart (1-minute).
   A green badge appears at the bottom-left: `TrendIkaterr watcher · NIFTY 50: 🔵 BULLISH · Bot connected`.
3. Keep that Chrome window visible (not minimised, not hidden behind a full-screen app) and the mouse off
   the chart. Chrome stops updating hidden pages; the bot warns you if it can't see the chart for a minute.

**What it does** (rules under `[chart_watch]` in `config.toml`)
- `follow = "options"` (default): every Call and Put chart on screen is its own signal. When a 1-minute candle
  closes **blue** on one, alert (pop-up, voice, Telegram) and a **paper** buy of that option, 1 lot. When it
  closes **red**, paper sell. Several trades a day; never more than ₹20,000 in open trades at once.
- `follow = "nifty"`: only the Nifty 50 chart; blue buys an in-the-money call, red sells it.
- Also sells at a 20% stop, a 20% trailing stop, or 15:15. Stops buying for the day at ₹5,000 profit,
  ₹3,000 loss or 4 losing trades. Set `auto_buy = false` for alerts only.
- Paper trades go into `trades.csv` with mode `paper-chart`. **No real orders**: live trading from the chart
  is not switched on.

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
