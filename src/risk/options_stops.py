"""Five-layer stop ladder for long single-leg options positions.

Per scan tick, every open options position is evaluated through:

1. **EOD square-off** — hard exit at ``options_eod_squareoff`` (15:05 IST).
2. **Underlying point cap** — exit if spot moves N points adverse:
   - NIFTY: 50 pts
   - BANKNIFTY: 125 pts
3. **Premium SL** — exit if premium drops 35% from entry (Phase 1) OR
   premium drops below the trailing SL (Phase 2/3 — see below).
4. **Time stop** — exit if held ≥ 45 min AND underlying moved < 0.5×ATR.
5. **Take profit** — handled implicitly: there is NO hard TP.
   Once premium reaches +35% (1:1), SL ratchets to entry premium
   ("breakeven lock"). Beyond that, SL trails at 30% below the
   high-water-mark premium. Position exits when the trail catches
   the reversal — capturing 1:2, 1:3, 1:4 if the move keeps running.

Returns an ``OptionsExit`` dataclass with the triggered reason or
``None`` if no stop fired (caller holds).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from config.settings import StrategyCfg

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class OptionsExit:
    reason: str          # eod_squareoff | underlying_pts | premium_stop | trail_stop | time_stop
    sell_premium: float  # premium to sell at (caller applies slippage)
    note: str = ""


def _underlying_pts_cap(underlying: str, cfg: StrategyCfg) -> float:
    if underlying == "NIFTY":
        return float(cfg.options_underlying_stop_pts_nifty)
    if underlying == "BANKNIFTY":
        return float(cfg.options_underlying_stop_pts_banknifty)
    # Conservative fallback for any future underlying.
    return 100.0


def _eod_time(cfg: StrategyCfg) -> dtime:
    hh, mm = map(int, cfg.options_eod_squareoff.split(":"))
    return dtime(hh, mm)


def _stop_level(
    pos: dict,
    high_water: float,
    breakeven_locked: bool,
    cfg: StrategyCfg,
) -> float:
    """Current trailing SL premium for this position.

    Phase 1 (not yet at 1:1): SL = entry × (1 - premium_stop_pct/100)
    Phase 2 (just hit 1:1):   SL = entry premium (breakeven lock)
    Phase 3 (running):        SL = max(entry, high_water × (1 - trailing_pct/100))
    """
    entry = pos["entry_premium"]
    if not breakeven_locked:
        return entry * (1 - cfg.options_premium_stop_pct / 100.0)
    # Breakeven-locked → never below entry.
    trail_floor = high_water * (1 - cfg.options_trailing_premium_pct / 100.0)
    return max(entry, trail_floor)


def update_high_water_and_breakeven(
    pos: dict, current_premium: float, cfg: StrategyCfg,
) -> tuple[float, bool]:
    """Compute the new high_water_mark + breakeven_locked flag.

    high_water ratchets up; never down.
    breakeven flips from False→True the first tick premium hits +breakeven_pct
    above entry. Once True, stays True (one-way ratchet).
    """
    entry = pos["entry_premium"]
    new_high = max(float(pos.get("high_water_premium", entry)), current_premium)
    one_to_one = entry * (1 + cfg.options_premium_breakeven_pct / 100.0)
    new_be = bool(pos.get("breakeven_locked", 0)) or (new_high >= one_to_one)
    return new_high, new_be


def check_options_exit(
    pos: dict,
    current_premium: float,
    current_spot: float,
    now: datetime,
    cfg: StrategyCfg,
    *,
    underlying_atr: float = 0.0,
) -> OptionsExit | None:
    """Apply the 5-layer ladder. Returns the first triggered exit or None.

    ``pos`` is a dict from ``StateStore.load_options_positions()``.
    ``current_premium`` and ``current_spot`` come from the live data
    fetcher. ``underlying_atr`` is optional (used by the time stop's
    "stalled" check); pass 0 to disable the 0.5×ATR gate.
    """
    # 1. EOD — non-negotiable.
    now_t = now.astimezone(IST).timetz() if now.tzinfo else now.replace(tzinfo=IST).timetz()
    if now_t.replace(tzinfo=None) >= _eod_time(cfg):
        return OptionsExit(reason="eod_squareoff", sell_premium=current_premium,
                           note=f"now={now_t.replace(tzinfo=None).strftime('%H:%M')}")

    # 2. Underlying point cap — direction depends on CE vs PE.
    pts_cap = _underlying_pts_cap(pos["underlying"], cfg)
    if pos["option_type"] == "CE":   # long call hurts when spot drops
        adverse = pos["entry_spot"] - current_spot
    else:                              # PE — long put hurts when spot rises
        adverse = current_spot - pos["entry_spot"]
    if adverse >= pts_cap:
        return OptionsExit(
            reason="underlying_pts",
            sell_premium=current_premium,
            note=f"adverse={adverse:.0f}pts ≥ cap={pts_cap:.0f}",
        )

    # 3. Premium / trail SL.
    high_water, breakeven = update_high_water_and_breakeven(pos, current_premium, cfg)
    sl = _stop_level(pos, high_water, breakeven, cfg)
    if current_premium <= sl:
        reason = "trail_stop" if breakeven else "premium_stop"
        return OptionsExit(
            reason=reason,
            sell_premium=current_premium,
            note=f"premium={current_premium:.2f} ≤ sl={sl:.2f} (high={high_water:.2f}, be={breakeven})",
        )

    # 4. Time stop — held > N min AND spot moved < 0.5×ATR.
    opened_at = datetime.fromisoformat(pos["opened_at"])
    age_min = (now - opened_at).total_seconds() / 60.0
    if age_min >= cfg.options_time_stop_minutes:
        if underlying_atr > 0:
            move = abs(current_spot - pos["entry_spot"])
            if move <= 0.5 * underlying_atr:
                return OptionsExit(
                    reason="time_stop",
                    sell_premium=current_premium,
                    note=f"age={age_min:.0f}m, move={move:.0f}pts ≤ 0.5×atr={underlying_atr*0.5:.0f}",
                )
    return None
