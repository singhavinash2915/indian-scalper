# Pine Script — TradingView cross-check

Independent TradingView implementation of the bot's 8-factor scorer
so you can eyeball whether the two agree.

## Files

| File | What it is |
|---|---|
| `indian-scalper-scorer.pine` | The Pine Script v5 indicator. Paste into TradingView's Pine editor. |
| `README.md` | This doc. |

## One-time setup

1. Open any NSE symbol chart in TradingView — **15-minute timeframe**
   (matches the bot's `strategy.candle_interval = 15m`).
2. `Pine Editor` tab → paste the full contents of
   `indian-scalper-scorer.pine` → `Save`  → `Add to chart`.
3. Optional: favourite the script so you don't have to re-paste.

You now see a sub-pane under the candles showing the 0–8 score as a
step line, colour-coded:

- **Green background** — score ≥ 6 (bot entry threshold by default)
- **Amber** — 4–5 (close but not firing)
- **Grey** — < 4
- **Magenta** — RSI > 78 hard block (bot refuses to enter even on 8/8)

A table in the top-right of the sub-pane lists the 8 factors individually
with ✓ / · marks.

## Setting up the alert

1. Right-click the score line → `Add alert on Indian Scalper Scorer`.
2. **Condition**: `Score crossed entry threshold`.
3. **Notification**: Email / Webhook / Mobile push — your call.
4. Repeat once per symbol you want tracked.

TradingView Paid tiers allow multiple alerts; Free tier gives you 1.

## Cross-checking against the bot

The point of this script is **independent verification**, not
automation. Two checkpoints:

1. **Daily eyeball.** Open the same symbol on both dashboards (bot's
   `/signals` chart drawer + TradingView). The score in the top-right
   table cells should match. Minor timing differences (EMA warmup,
   last-bar VWAP) are expected; consistent multi-factor disagreement
   is a bug worth investigating.

2. **After any threshold change in `config.yaml`.** If you lower
   `min_score` from 6 to 5, both need the update — this Pine is NOT
   auto-synced to config. Update the `min_score` input in the
   indicator settings + the `strategy.min_score` in config.yaml.

## Known divergences that are NOT bugs

- **First ~100 bars of a fresh chart.** pandas-ta's EMA/RSI/ATR warm
  up from the first bar; Pine's builtins do the same but with slightly
  different initial-condition conventions. Scores may differ 0–1 points
  early in the day; converge after warmup.

- **Last-bar volume surge.** NSE volume via yfinance (bot) vs. TV is
  delayed differently. The most recent bar's `volume_surge` factor
  can flip between checks; the bar before should agree.

- **Pre-market / extended-hours bars.** Bot ignores them (market-hours
  gate); TV may show them. Only compare bars inside 09:15–15:30 IST.

## Keeping the Pine in sync

When you change anything in `src/strategy/scoring.py` or
`config.yaml` strategy thresholds, update the Pine header's "Derived
from commit" line + the input defaults in the script. A drift of
more than 3 months between bot and Pine is asking for trouble.

## Paper-week workflow

Suggested daily routine while you're building conviction in the bot:

- **Before 09:30 IST** — bot finishes its first scan tick. Note the
  top 3 symbols by score in the `/signals` tab.
- **In TV** — add those 3 to your watchlist with the indicator
  applied. Verify the score matches (±1 is fine).
- **Mid-day** — if any symbol hits score ≥ 6 on the bot, you'll get
  the corresponding TV alert (assuming you've set it up per-symbol).
  If one fires on TV but NOT on the bot, or vice versa, that's the
  moment to flip mode → `watch_only` and investigate.
