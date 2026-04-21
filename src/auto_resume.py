"""scalper-auto-resume — weekday 09:14 IST auto-start + 15:30 auto-pause.

Driven by launchd (macOS) or cron (Linux). The caller wakes this
script at various candidate times; the script itself decides whether
to act based on the current IST wall-clock.

Five guards must all pass before we flip ``scheduler_state``:

    1. **Opt-in** — ``control_flags.auto_resume_enabled`` must be ``"1"``.
       Default is ``"0"``. Operators turn this on via the dashboard
       toggle once they've confirmed a few days of manual operation
       behave correctly.
    2. **Weekday** — NSE is closed Sat/Sun.
    3. **Trading day** — ``HolidayCalendar.is_trading_day(today)`` must
       be True. Ties us to the shipped holidays YAML + operator
       maintenance of it.
    4. **Kill switch armed** — if the kill switch is tripped from a
       prior incident, refuse to auto-resume. Operator must
       investigate + re-arm manually.
    5. **Trade mode sane** — must be ``paper`` or ``live``. Refusing
       on ``watch_only`` because auto-resuming into watch-only does
       nothing useful (scheduler runs but never trades) and likely
       means the operator forgot to flip back to paper.

``--action`` selects the action after the guards pass:

    resume   — set scheduler_state=running
    pause    — set scheduler_state=paused (no guards 2–5; just opt-in
               and weekday, so EOD pause still fires if you've been
               running)

Exit codes:

    0  — acted, or declined cleanly
    1  — acted but the target state was already set (idempotent no-op)
    2  — guard failed (logged to stderr)
    3  — unexpected error

``--now ISO`` for tests: pin the simulated current time instead of
using wall clock.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Wake-window half-width: the caller can fire the script anywhere
# within ±FIRE_TOLERANCE_MINUTES of the target time and we'll still
# act. Outside that window we exit quietly. Tuned so launchd's
# inherent slop (up to ~10s) doesn't miss the fire, and a
# minute-fires-anything cron doesn't act 60× per day.
FIRE_TOLERANCE_MINUTES = 2

# IST wall-clock targets.
RESUME_TARGET = (9, 14)  # 09:14 IST — 1 minute before market open
PAUSE_TARGET = (15, 30)  # 15:30 IST — market close


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scalper-auto-resume", description=__doc__)
    ap.add_argument("--config", default="config.yaml", type=Path)
    ap.add_argument(
        "--action", choices=("resume", "pause"), required=True,
        help="Which transition to attempt after guards pass.",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="Bypass the fire-window check (still runs all 5 guards). "
             "Used by tests and ad-hoc operator commands.",
    )
    ap.add_argument(
        "--now", help="ISO timestamp for simulated now (test hook).",
    )
    args = ap.parse_args(argv)

    # --- deferred imports to dodge the brokers↔execution circular ---
    import brokers.base  # noqa: F401
    from config.settings import Settings
    from data.holidays import DEFAULT_YAML_PATH, HolidayCalendar
    from execution.state import StateStore
    from loguru import logger

    if args.now:
        try:
            now_ist = datetime.fromisoformat(args.now)
            if now_ist.tzinfo is None:
                now_ist = now_ist.replace(tzinfo=IST)
            else:
                now_ist = now_ist.astimezone(IST)
        except ValueError:
            print(f"error: --now {args.now!r} is not a valid ISO timestamp")
            return 3
    else:
        now_ist = datetime.now(IST)

    # 0. Fire-window check (unless --force).
    target = RESUME_TARGET if args.action == "resume" else PAUSE_TARGET
    if not args.force and not _within_fire_window(now_ist, target):
        # Quiet exit — the caller fires us at multiple candidate times
        # to cover timezone + launchd-slop; most fires are no-ops.
        return 0

    try:
        settings = Settings.load(args.config)
    except Exception as exc:
        _stderr(f"auto-resume: config load failed: {exc}")
        return 3

    db_path = settings.raw.get("storage", {}).get("db_path", "data/scalper.db")
    store = StateStore(db_path)

    # --- guards -------------------------------------------------------
    # 1. Opt-in.
    if store.get_flag("auto_resume_enabled", "0") != "1":
        logger.info("auto-resume: opt-in flag not set; exiting")
        return 0

    # 2. Weekday.
    if now_ist.weekday() >= 5:  # Sat/Sun
        logger.info(
            "auto-resume: {} is a weekend; exiting", now_ist.strftime("%A"),
        )
        return 0

    # The remaining guards only apply to 'resume'. Pause should fire
    # at 15:30 even on a holiday if the scheduler somehow got left
    # running — conservative side.
    if args.action == "resume":
        # 3. Trading day (NSE holiday calendar).
        try:
            cal = HolidayCalendar(db_path)
            cal.load_from_yaml(DEFAULT_YAML_PATH)
            if not cal.is_trading_day(now_ist.date()):
                _stderr(
                    f"auto-resume: {now_ist.date().isoformat()} is an NSE "
                    "holiday; not resuming",
                )
                return 2
        except Exception as exc:
            _stderr(f"auto-resume: holiday check failed: {exc}")
            return 3

        # 4. Kill switch.
        if store.get_flag("kill_switch", "armed") != "armed":
            _stderr(
                "auto-resume: kill_switch is tripped; refusing to resume. "
                "Re-arm manually via the dashboard first.",
            )
            return 2

        # 5. Trade mode.
        mode = store.get_flag("trade_mode", "watch_only")
        if mode not in ("paper", "live"):
            _stderr(
                f"auto-resume: trade_mode={mode!r}; refusing to auto-resume "
                "(resume on watch_only is a no-op). Flip mode to paper or live "
                "via the dashboard first.",
            )
            return 2

    # --- act ----------------------------------------------------------
    current = store.get_flag("scheduler_state", "stopped")
    target_state = "running" if args.action == "resume" else "paused"
    if current == target_state:
        logger.info(
            "auto-{}: scheduler_state already {}; no-op", args.action, current,
        )
        return 1

    store.set_flag(
        "scheduler_state", target_state, actor="auto_resume",
    )
    logger.info(
        "auto-{}: scheduler_state {} → {}",
        args.action, current, target_state,
    )
    return 0


def _within_fire_window(now_ist: datetime, target_hm: tuple[int, int]) -> bool:
    """True if ``now_ist`` is within ±FIRE_TOLERANCE_MINUTES of
    ``target_hm`` IST."""
    h, m = target_hm
    target_today = now_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = abs((now_ist - target_today).total_seconds())
    return delta <= FIRE_TOLERANCE_MINUTES * 60


def _stderr(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


if __name__ == "__main__":
    sys.exit(main())
