"""
options_live.py
---------------
Continuous live signal engine for NIFTY / SENSEX / BANKNIFTY options.

What it does:
  1. Every POLL_INTERVAL_SEC seconds during market hours, generates a
     directional signal on each underlying (BUY_CALL / BUY_PUT / NO_TRADE)
     using the same 5-of-5 confirmation strategy as options_signal.py.
  2. Tracks "virtual positions" — when a fresh signal fires, it records
     the entry spot, stop level, target level, and contract details.
  3. Sends a Telegram alert for every event:
       * NEW ENTRY  — first time direction goes long-CALL or long-PUT
       * STOP HIT   — spot moves past the stop level
       * TARGET HIT — spot reaches the 2:1 reward level
       * SIGNAL FLIP — direction reverses (e.g. CALL → PUT)
       * SQUARE-OFF — any open position closed at 15:15 IST
  4. Logs all events to logs/options.csv.

Position tracking is purely for *alerting*. No actual orders are placed
(you'd execute them in your broker terminal based on the Telegram alerts).

Run:    python options_live.py
Stop:   Ctrl+C
"""

from __future__ import annotations

import csv
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, Optional

import pytz

import config
from logger import log, send_telegram
from options_signal import (
    UNDERLYINGS,
    Underlying,
    generate_signal,
    OptionsSignal,
)

IST = pytz.timezone("Asia/Kolkata")

# ----------------------------------------------------------------------------
# Loop config
# ----------------------------------------------------------------------------
POLL_INTERVAL_SEC = 300        # 5 minutes (matches 5-min candle cadence)
TICK_CHECK_SEC = 30            # how often to poll spot to check stop/target hits
SQUAREOFF_HHMM = config.TIMING.squareoff_hhmm   # 15:15 IST
MARKET_OPEN_HHMM = config.TIMING.market_open_hhmm
MARKET_CLOSE_HHMM = config.TIMING.market_close_hhmm

OPTIONS_LOG_CSV = os.path.join(config.LOG_DIR, "options.csv")
OPTIONS_LOG_FIELDS = [
    "timestamp_ist", "event", "underlying", "direction", "contract",
    "spot", "entry_spot", "stop_spot", "target_spot", "pnl_pct", "reason",
]


# ----------------------------------------------------------------------------
# Virtual position state per underlying
# ----------------------------------------------------------------------------
@dataclass
class VirtualPosition:
    underlying: str        # "NIFTY" / "SENSEX" / "BANKNIFTY"
    direction: str         # "BUY_CALL" / "BUY_PUT"
    contract: str          # e.g. "NIFTY 28MAY26 23650 CE"
    entry_spot: float
    stop_spot: float
    target_spot: float
    entry_time: datetime
    expiry: datetime
    strike: int
    lot_size: int

    # Highest favorable spot since entry — used for trailing math (future)
    high_water_spot: float = 0.0


# Live state — one slot per underlying
POSITIONS: Dict[str, VirtualPosition] = {}


# ----------------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------------
def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    n = now_ist().time()
    return dtime(*MARKET_OPEN_HHMM) <= n <= dtime(*MARKET_CLOSE_HHMM)


def is_squareoff_time() -> bool:
    return now_ist().time() >= dtime(*SQUAREOFF_HHMM)


# ----------------------------------------------------------------------------
# CSV logging
# ----------------------------------------------------------------------------
def _ensure_log_dir():
    os.makedirs(config.LOG_DIR, exist_ok=True)


def log_event(event: str, underlying: str, **kw) -> None:
    """Append a row to logs/options.csv."""
    _ensure_log_dir()
    write_header = not os.path.exists(OPTIONS_LOG_CSV)
    row = {
        "timestamp_ist": now_ist().isoformat(timespec="seconds"),
        "event": event,
        "underlying": underlying,
    }
    row.update(kw)
    with open(OPTIONS_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OPTIONS_LOG_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in OPTIONS_LOG_FIELDS})


# ----------------------------------------------------------------------------
# Alert formatting
# ----------------------------------------------------------------------------
def fmt_contract(sig: OptionsSignal) -> str:
    u = sig.underlying
    suffix = "CE" if sig.direction == "BUY_CALL" else "PE"
    return f"{u.name} {sig.expiry.strftime('%d%b%y').upper()} {sig.strike} {suffix}"


def send_entry_alert(sig: OptionsSignal, contract: str) -> None:
    side = "CALL" if sig.direction == "BUY_CALL" else "PUT"
    emoji = "🟢" if side == "CALL" else "🔴"
    arrow = "above" if side == "PUT" else "below"
    sl_pct = abs((sig.stop_spot / sig.spot - 1) * 100)
    tg_pct = abs((sig.target_spot / sig.spot - 1) * 100)
    msg = (
        f"{emoji} <b>BUY {side} — {sig.underlying.name}</b>\n"
        f"<b>Contract:</b> {contract}\n"
        f"<b>Spot:</b> {sig.spot:,.2f}\n"
        f"<b>Stop:</b> exit if spot {arrow} {sig.stop_spot:,.2f}  ({sl_pct:.2f}%)\n"
        f"<b>Target:</b> {sig.target_spot:,.2f}  ({tg_pct:.2f}%)\n"
        f"<b>Lot size:</b> {sig.underlying.lot_size}\n"
        f"<b>Expiry:</b> {sig.expiry.strftime('%d-%b-%Y (%a)')}\n"
        f"<b>Why:</b> {sig.reason}\n"
        f"<i>Score: {sig.score}/5 confirmations</i>"
    )
    send_telegram(msg)


def send_exit_alert(pos: VirtualPosition, current_spot: float, reason: str) -> None:
    # P&L proxy: spot move in your favor, scaled by ~delta 0.5 (ATM)
    if pos.direction == "BUY_CALL":
        spot_move = current_spot - pos.entry_spot
        pnl_pct_on_spot = (current_spot / pos.entry_spot - 1) * 100
    else:
        spot_move = pos.entry_spot - current_spot
        pnl_pct_on_spot = (pos.entry_spot / current_spot - 1) * 100
    sign = "🟢" if spot_move >= 0 else "🔴"
    side = "CALL" if pos.direction == "BUY_CALL" else "PUT"
    msg = (
        f"{sign} <b>EXIT {side} — {pos.underlying}</b>\n"
        f"<b>Contract:</b> {pos.contract}\n"
        f"<b>Entry spot:</b> {pos.entry_spot:,.2f}\n"
        f"<b>Exit spot:</b> {current_spot:,.2f}\n"
        f"<b>Spot Δ:</b> {spot_move:+,.2f}  ({pnl_pct_on_spot:+.2f}% spot)\n"
        f"<b>Premium P&amp;L est:</b> ≈ {spot_move * 0.5:+,.2f} per unit "
        f"(delta 0.5)  →  ≈ ₹{spot_move * 0.5 * pos.lot_size:+,.0f} / lot\n"
        f"<b>Reason:</b> {reason}"
    )
    send_telegram(msg)


# ----------------------------------------------------------------------------
# Position lifecycle
# ----------------------------------------------------------------------------
def open_position(sig: OptionsSignal) -> VirtualPosition:
    contract = fmt_contract(sig)
    pos = VirtualPosition(
        underlying=sig.underlying.name,
        direction=sig.direction,
        contract=contract,
        entry_spot=sig.spot,
        stop_spot=sig.stop_spot,
        target_spot=sig.target_spot,
        entry_time=now_ist(),
        expiry=sig.expiry,
        strike=sig.strike,
        lot_size=sig.underlying.lot_size,
        high_water_spot=sig.spot,
    )
    POSITIONS[sig.underlying.name] = pos
    send_entry_alert(sig, contract)
    log_event(
        "ENTRY", sig.underlying.name,
        direction=sig.direction, contract=contract,
        spot=round(sig.spot, 2), entry_spot=round(sig.spot, 2),
        stop_spot=round(sig.stop_spot, 2), target_spot=round(sig.target_spot, 2),
        reason=sig.reason,
    )
    log.info(f"OPTIONS ENTRY: {contract} @ spot {sig.spot:.2f}  "
             f"SL {sig.stop_spot:.2f}  TGT {sig.target_spot:.2f}")
    return pos


def close_position(name: str, current_spot: float, reason: str) -> None:
    pos = POSITIONS.pop(name, None)
    if not pos:
        return
    send_exit_alert(pos, current_spot, reason)
    # P&L percentage on spot (rough — actual premium P&L depends on IV/theta)
    if pos.direction == "BUY_CALL":
        pnl_pct = (current_spot / pos.entry_spot - 1) * 100
    else:
        pnl_pct = (pos.entry_spot / current_spot - 1) * 100
    log_event(
        "EXIT", name,
        direction=pos.direction, contract=pos.contract,
        spot=round(current_spot, 2), entry_spot=round(pos.entry_spot, 2),
        stop_spot=round(pos.stop_spot, 2), target_spot=round(pos.target_spot, 2),
        pnl_pct=round(pnl_pct, 3), reason=reason,
    )
    log.info(f"OPTIONS EXIT:  {pos.contract} @ spot {current_spot:.2f}  "
             f"reason={reason}  pnl={pnl_pct:+.2f}%")


# ----------------------------------------------------------------------------
# Per-tick checks (stop, target) — runs more often than signal regen
# ----------------------------------------------------------------------------
def check_stops_and_targets(latest_spots: Dict[str, float]) -> None:
    for name in list(POSITIONS.keys()):
        spot = latest_spots.get(name)
        if spot is None:
            continue
        pos = POSITIONS[name]
        # Update high water mark (for future trailing logic)
        if pos.direction == "BUY_CALL" and spot > pos.high_water_spot:
            pos.high_water_spot = spot
        elif pos.direction == "BUY_PUT" and spot < pos.high_water_spot:
            pos.high_water_spot = spot

        # CALL: stop if spot drops below stop_spot, target if rises above
        if pos.direction == "BUY_CALL":
            if spot <= pos.stop_spot:
                close_position(name, spot, "STOP_HIT")
            elif spot >= pos.target_spot:
                close_position(name, spot, "TARGET_HIT")
        # PUT: stop if spot rises above stop_spot, target if drops below
        else:
            if spot >= pos.stop_spot:
                close_position(name, spot, "STOP_HIT")
            elif spot <= pos.target_spot:
                close_position(name, spot, "TARGET_HIT")


# ----------------------------------------------------------------------------
# Signal regeneration — every POLL_INTERVAL_SEC
# ----------------------------------------------------------------------------
def evaluate_signals() -> Dict[str, float]:
    """Generate fresh signals on each underlying, manage entries / flips.
    Returns dict of {underlying_name: current_spot} for stop/target checks."""
    latest_spots: Dict[str, float] = {}
    for u in UNDERLYINGS:
        try:
            sig = generate_signal(u)
        except Exception as e:
            log.warning(f"Signal generation failed for {u.name}: {e}")
            continue

        if sig.spot > 0:
            latest_spots[u.name] = sig.spot

        current_pos = POSITIONS.get(u.name)

        if sig.direction in ("BUY_CALL", "BUY_PUT"):
            if current_pos is None:
                # Fresh entry
                open_position(sig)
            elif current_pos.direction != sig.direction:
                # Signal flipped — exit old, enter new
                close_position(u.name, sig.spot, "SIGNAL_FLIPPED")
                open_position(sig)
            # else: same direction, still in trade — nothing to do
        elif sig.direction == "NO_TRADE":
            # We could choose to exit on NO_TRADE; but that's noisy because
            # the strategy needs 4/5 confirmations and one indicator easing
            # back to 3/5 would close a perfectly good trade. So we only
            # exit on explicit signal flip, stop, target, or 15:15.
            pass

    return latest_spots


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
_running = True


def _handle_sigint(signum, frame):
    global _running
    log.info(f"Caught signal {signum}, shutting down...")
    _running = False


def run():
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        signal.signal(signal.SIGTERM, _handle_sigint)
    except (AttributeError, ValueError, OSError):
        pass

    log.info("=" * 60)
    log.info("Options live signal engine starting")
    log.info(f"Underlyings: {[u.name for u in UNDERLYINGS]}")
    log.info(f"Poll interval: {POLL_INTERVAL_SEC}s  |  "
             f"Tick check interval: {TICK_CHECK_SEC}s")
    log.info(f"Market window: {MARKET_OPEN_HHMM} - {MARKET_CLOSE_HHMM} IST")
    log.info(f"Square-off: {SQUAREOFF_HHMM} IST")
    log.info("=" * 60)
    send_telegram(
        "🤖 <b>Options signal engine started</b>\n"
        f"Watching: NIFTY, BANKNIFTY, SENSEX\n"
        f"Strategy: 4-of-5 confirmation (VWAP, EMA, RSI, MACD, Supertrend)\n"
        f"Poll: every {POLL_INTERVAL_SEC // 60} min  |  Stop checks: every {TICK_CHECK_SEC}s"
    )

    last_signal_eval = 0.0
    latest_spots: Dict[str, float] = {}

    while _running:
        try:
            # Square-off check first (runs even outside market hours so we don't
            # carry positions across days)
            if is_squareoff_time() and POSITIONS:
                log.info("Square-off time reached — closing all positions")
                for name in list(POSITIONS.keys()):
                    spot = latest_spots.get(name, POSITIONS[name].entry_spot)
                    close_position(name, spot, "DAILY_SQUAREOFF_315PM")

            if not is_market_open():
                # Sleep longer when market is closed
                time.sleep(30)
                continue

            now = time.time()

            # Regenerate signals every POLL_INTERVAL_SEC
            if now - last_signal_eval >= POLL_INTERVAL_SEC:
                latest_spots = evaluate_signals()
                last_signal_eval = now

            # Per-tick check (frequent, just spot vs stop/target)
            if POSITIONS:
                # Re-pull just the spot for active underlyings (cheap)
                for name in list(POSITIONS.keys()):
                    u = next((x for x in UNDERLYINGS if x.name == name), None)
                    if not u:
                        continue
                    try:
                        # Reuse signal generator — gives us spot quickly
                        sig = generate_signal(u)
                        if sig.spot > 0:
                            latest_spots[name] = sig.spot
                    except Exception:
                        pass
                check_stops_and_targets(latest_spots)

            time.sleep(TICK_CHECK_SEC)
        except Exception as e:
            log.exception(f"Loop iteration failed: {e}")
            time.sleep(TICK_CHECK_SEC)

    # Graceful shutdown — square off everything if needed
    if POSITIONS:
        log.info("Shutting down — closing remaining positions")
        for name in list(POSITIONS.keys()):
            spot = latest_spots.get(name, POSITIONS[name].entry_spot)
            close_position(name, spot, "SHUTDOWN")
    send_telegram("🛑 <b>Options signal engine stopped</b>")


if __name__ == "__main__":
    run()
