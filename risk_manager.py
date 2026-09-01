"""
risk_manager.py
---------------
Owns ALL money-management decisions. The strategy module decides
"is this a setup?" — this module decides "can we take it, and if so how big?"

Responsibilities:
  * Position sizing from capital + risk-per-trade
  * Stop-loss / target price computation
  * Trailing stop-loss bookkeeping (per position)
  * Daily realized + unrealized P&L tracking
  * Hard daily loss limit → kill switch for new entries
  * 15:15 IST forced square-off check
  * Max-concurrent-positions cap

Keeping all this in one place means there's exactly ONE source of truth
for whether a trade can happen. Other modules should never bypass this.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, List, Optional

import pytz

import config
from logger import log

IST = pytz.timezone("Asia/Kolkata")


# ----------------------------------------------------------------------------
# Position state
# ----------------------------------------------------------------------------
@dataclass
class Position:
    symbol: str
    quantity: int
    entry_price: float
    stop_loss: float            # current (possibly trailed) stop price
    initial_stop: float         # original stop at entry — for reference
    target: float
    entry_time: datetime
    # Highest price seen since entry — used by the trailing-stop math
    high_water_mark: float = 0.0
    # Most recent broker order id (for modify/cancel)
    order_id: str = ""

    @property
    def risk_per_share(self) -> float:
        return self.entry_price - self.initial_stop


@dataclass
class _DailyState:
    realized_pnl: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""
    # Reset marker — the date we last reset for
    session_date: Optional[datetime.date] = None


class RiskManager:
    """Single instance shared by main.py. Thread-safety is not needed —
    the main loop is single-threaded and websocket callbacks only push
    ticks into a queue; risk decisions all happen on the main thread."""

    def __init__(self) -> None:
        self.positions: Dict[str, Position] = {}
        self.daily = _DailyState()
        self._reset_if_new_session()

    # ------------------------------------------------------------------ #
    # Session bookkeeping
    # ------------------------------------------------------------------ #
    def _reset_if_new_session(self) -> None:
        """Reset daily P&L tracker if the IST date has rolled over."""
        today = datetime.now(IST).date()
        if self.daily.session_date != today:
            log.info(f"New trading session: {today} — resetting daily state")
            self.daily = _DailyState(session_date=today)

    # ------------------------------------------------------------------ #
    # Sizing
    # ------------------------------------------------------------------ #
    def compute_stop_and_target(self, entry_price: float) -> tuple[float, float]:
        """Returns (stop_loss_price, target_price) for a LONG entry."""
        sl_dist = entry_price * config.RISK.stop_loss_pct
        stop = round(entry_price - sl_dist, 2)
        target = round(entry_price + sl_dist * config.RISK.reward_to_risk, 2)
        return stop, target

    def compute_position_size(self, entry_price: float, stop_price: float) -> int:
        """Size the position so a stop-out loses exactly `risk_per_trade_pct`
        of capital. Returns 0 if the math says we can't afford even 1 share.
        """
        if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
            return 0
        risk_rupees = config.RISK.capital * config.RISK.risk_per_trade_pct
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return 0
        qty = math.floor(risk_rupees / risk_per_share)
        # Also cap by capital available — never spend more than capital on one trade
        max_affordable = math.floor(config.RISK.capital / entry_price)
        qty = min(qty, max_affordable)
        return max(qty, 0)

    # ------------------------------------------------------------------ #
    # Pre-entry gating
    # ------------------------------------------------------------------ #
    def can_open_new_position(self, symbol: str) -> tuple[bool, str]:
        """Returns (allowed, reason_if_denied)."""
        self._reset_if_new_session()

        if self.daily.halted:
            return False, f"halted: {self.daily.halt_reason}"

        if symbol in self.positions:
            return False, "already_have_position"

        if len(self.positions) >= config.RISK.max_open_positions:
            return False, f"max_positions ({config.RISK.max_open_positions}) reached"

        if not is_trading_window_open():
            return False, "outside_trading_window"

        if is_too_close_to_squareoff():
            return False, "too_close_to_squareoff"

        return True, ""

    # ------------------------------------------------------------------ #
    # Position lifecycle
    # ------------------------------------------------------------------ #
    def open_position(self, symbol: str, quantity: int, entry_price: float,
                      stop: float, target: float, order_id: str = "") -> Position:
        pos = Position(
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop,
            initial_stop=stop,
            target=target,
            entry_time=datetime.now(IST),
            high_water_mark=entry_price,
            order_id=order_id,
        )
        self.positions[symbol] = pos
        self.daily.trades_today += 1
        return pos

    def close_position(self, symbol: str, exit_price: float) -> float:
        """Realize P&L for an exit. Returns realized P&L (rupees)."""
        if symbol not in self.positions:
            return 0.0
        pos = self.positions.pop(symbol)
        pnl = (exit_price - pos.entry_price) * pos.quantity
        self.daily.realized_pnl += pnl
        self._check_daily_loss_limit()
        return pnl

    # ------------------------------------------------------------------ #
    # Per-tick risk checks (trailing stop, stop-loss hit, target hit)
    # ------------------------------------------------------------------ #
    def update_trailing_stop(self, symbol: str, last_price: float) -> bool:
        """Walk the stop up as price moves in our favor. Returns True if
        the stop was modified (caller may want to log/modify broker order).
        """
        pos = self.positions.get(symbol)
        if not pos:
            return False

        # Track highest price seen
        if last_price > pos.high_water_mark:
            pos.high_water_mark = last_price

        # Only start trailing once price has moved enough above entry
        activation_price = pos.entry_price * (1 + config.RISK.trail_activate_pct)
        if pos.high_water_mark < activation_price:
            return False

        # Trail by the original stop-loss distance below the high water mark
        new_stop = round(pos.high_water_mark - pos.risk_per_share, 2)
        if new_stop > pos.stop_loss:
            log.info(
                f"Trailing stop {symbol}: {pos.stop_loss:.2f} → {new_stop:.2f} "
                f"(hwm={pos.high_water_mark:.2f})"
            )
            pos.stop_loss = new_stop
            return True
        return False

    def check_stop_or_target_hit(self, symbol: str, last_price: float) -> Optional[str]:
        """Returns 'STOP', 'TARGET', or None. Caller should immediately
        place an exit order if non-None."""
        pos = self.positions.get(symbol)
        if not pos:
            return None
        if last_price <= pos.stop_loss:
            return "STOP"
        if last_price >= pos.target:
            return "TARGET"
        return None

    # ------------------------------------------------------------------ #
    # Daily loss cap — kill switch
    # ------------------------------------------------------------------ #
    def _check_daily_loss_limit(self) -> None:
        limit = -abs(config.RISK.capital * config.RISK.daily_loss_limit_pct)
        if self.daily.realized_pnl <= limit and not self.daily.halted:
            self.daily.halted = True
            self.daily.halt_reason = (
                f"daily loss {self.daily.realized_pnl:.0f} ≤ limit {limit:.0f}"
            )
            log.error(f"DAILY LOSS LIMIT HIT — halting new entries. "
                      f"{self.daily.halt_reason}")

    def unrealized_pnl(self, last_prices: Dict[str, float]) -> float:
        """Mark-to-market across all open positions, using a dict of
        symbol → last_price provided by the caller."""
        total = 0.0
        for sym, pos in self.positions.items():
            ltp = last_prices.get(sym)
            if ltp is None:
                continue
            total += (ltp - pos.entry_price) * pos.quantity
        return total

    def total_pnl(self, last_prices: Dict[str, float]) -> float:
        return self.daily.realized_pnl + self.unrealized_pnl(last_prices)

    # ------------------------------------------------------------------ #
    # Forced square-off at 15:15 IST
    # ------------------------------------------------------------------ #
    def positions_to_squareoff(self) -> List[Position]:
        """Returns all open positions if we've crossed the square-off time."""
        if not is_squareoff_time():
            return []
        return list(self.positions.values())


# ============================================================================
# Module-level time helpers — pure functions so they're trivial to test
# ============================================================================
def _now_ist_time() -> time:
    return datetime.now(IST).time()


def is_trading_window_open() -> bool:
    open_t = time(*config.TIMING.market_open_hhmm)
    close_t = time(*config.TIMING.market_close_hhmm)
    return open_t <= _now_ist_time() <= close_t


def is_squareoff_time() -> bool:
    sq = time(*config.TIMING.squareoff_hhmm)
    return _now_ist_time() >= sq


def is_too_close_to_squareoff() -> bool:
    """True if we're within N minutes of the square-off time — used to
    block new entries that would barely have room to work."""
    now = datetime.now(IST)
    sq_h, sq_m = config.TIMING.squareoff_hhmm
    squareoff_dt = now.replace(hour=sq_h, minute=sq_m, second=0, microsecond=0)
    minutes_left = (squareoff_dt - now).total_seconds() / 60.0
    return minutes_left <= config.TIMING.no_new_entries_before_close_min
