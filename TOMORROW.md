# Wednesday 2026-04-22 — 09:15 IST Launch Cheat Sheet

Follow top to bottom. Zero cognitive load; deviations document in
`DRY_RUN_LOG.md`.

```
08:45  cd /Users/avinashsingh/Documents/indian-scalper
       uv run scalper                     # scheduler + dashboard
08:46  open http://127.0.0.1:8080
08:47  uv run scalper-preflight           # must show ≥10 PASS, 0 FAIL
                                           # (1 SKIP for live_credentials is fine)
08:48  Dashboard tab:
         equity = ₹5,00,000 · positions = 0 · trade_mode = PAPER
         equity curve has no points yet (first one lands on 09:30)
08:49  Universe tab: 97 enabled, 0 watch_only_override
08:50  Controls panel: STOPPED · ARMED · all mode + kill actions ready
09:10  Final sanity: news check — any earnings-call cancellations or
       market-moving events? If yes, stay STOPPED.
09:14  Controls → Resume → pill RUNNING. Kill switch untouched.
09:15  Market opens. No scan fires yet — first tick is at 09:30
       (skip_first_minutes=15 by config).
09:30  First scan runs. Signals tab populates with ~97 snapshots.
       Expect mostly skipped_score (Tuesday replay showed 0 entries
       on a typical day). 0 entries is FINE — not a bug.
```

## Quick-reference buttons

| Need to … | Do this |
|---|---|
| Pause scoring but keep LTP fresh | Controls → Pause |
| Kill everything immediately | Controls → KILL (3-second hold) |
| Turn bot off for the day | Same as KILL, then walk away |
| Flip to observe-only (no new entries, existing positions managed) | Mode switch → Watch |
| Disable a symbol for the day | Universe tab → row checkbox off |
| Inspect why a symbol scored N | Signals tab → click row → chart drawer |
| Understand what happened 10 minutes ago | Audit drawer (expand at bottom of Dashboard) |

## Things that should worry me

- [ ] **More than 5 positions open.** Max is 3 equity + 2 F&O. If
      this happens, there's a bug. KILL + investigate.
- [ ] **Any symbol with score ≥ 6 but action ≠ `entered`** with no
      obvious reason (sized to zero / position cap / watch-only
      override). Check the `reason` column; if it's opaque, that's
      a bug.
- [ ] **Equity curve drops > 1% in a single 15-min candle.** Check
      Open Positions, identify the culprit, consider KILL if it's
      behaving irrationally.
- [ ] **Dashboard becomes unresponsive.** SSH in, `lsof -i :8080` to
      confirm the process, `kill -9` only if positions are NOT at
      risk. Prefer `docker compose restart` / `systemctl restart
      indian-scalper` which re-runs preflight first.
- [ ] **Score distribution skews to 7–8/8 across the board.** This
      is the OPPOSITE of yesterday's replay — if it happens, suspect
      a look-ahead bug or stale-candle cache. Stay in Paper mode.

## End of day (15:20 IST auto → 15:30 close)

- 15:20 IST — EOD square-off fires automatically. Open Positions
  should drain within a minute or two.
- 15:30 IST — market closes. Review Signals + Trade History +
  Equity curve. Log lessons in a new `DAY_1_LOG.md` (gitignored).
- Leave the service running. Tomorrow you re-run this checklist.

## Emergency contacts (your future self)

```
SQLite emergency kill (if UI dead):
  uv run python -c "
  from execution.state import StateStore
  StateStore('data/scalper.db').set_flag('kill_switch', 'tripped', actor='cli-emergency')
  "

State dump (who did what today):
  sqlite3 data/scalper.db "
  SELECT ts, actor, action FROM operator_audit
  WHERE date(ts) = date('now', 'localtime')
  ORDER BY id DESC LIMIT 30;
  "
```
