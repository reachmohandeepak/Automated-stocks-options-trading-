"""
main.py
-------
Orchestrator. Wires together: data feed → indicators → strategy →
risk manager → order executor → logger → dashboard, and runs the
main event loop.

----------------------------------------------------------------------
SETUP (one-time)
----------------------------------------------------------------------
1. Install deps:        pip install -r requirements.txt
2. Copy .env.example → .env and fill in:
     - KITE_API_KEY, KITE_API_SECRET   (from developers.kite.trade)
     - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (optional)
3. Tune config.py — especially WATCHLIST, RISK.capital, EXECUTION.dry_run.

----------------------------------------------------------------------
DAILY (before market open)
----------------------------------------------------------------------
Kite's access_token expires every day at ~07:30 IST. Regenerate it:

  from kiteconnect import KiteConnect
  k = KiteConnect(api_key="...")
  print(k.login_url())            # open in browser, login, copy request_token
  data = k.generate_session("REQUEST_TOKEN", api_secret="...")
  print(data["access_token"])     # paste into .env as KITE_ACCESS_TOKEN

Then:  python main.py

----------------------------------------------------------------------
SAFETY
----------------------------------------------------------------------
config.EXECUTION.dry_run = True  ← KEEP THIS ON until you've watched
the bot run for a full session and are confident in its signals.
In dry-run, orders are logged but never sent to the broker.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime
from typing import Dict

import pytz

import config
import indicators as ind
from dashboard import Dashboard
from data_feed import build_feed
from logger import alert_exit, alert_trade, log, log_signal, log_trade, send_telegram
from order_executor import OrderExecutor
from risk_manager import (
    RiskManager,
    is_squareoff_time,
    is_trading_window_open,
)
from strategy import Signal, SignalType, evaluate

IST = pytz.timezone("Asia/Kolkata")


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------
class TradingBot:
    def __init__(self) -> None:
        log.info("=" * 60)
        log.info("Initializing trading bot")
        log.info(f"Broker: {config.BROKER}  DryRun: {config.EXECUTION.dry_run}")
        log.info(f"Watchlist: {config.WATCHLIST}")

        self.feed = build_feed(config.WATCHLIST)
        self.risk = RiskManager()
        # Pass the underlying broker client to the executor (may be None for non-Kite)
        kite_client = getattr(self.feed, "kite", None)
        self.executor = OrderExecutor(kite_client)
        self.dash = Dashboard(self.feed, self.risk)

        # Track the timestamp of the last candle we evaluated, so we only
        # re-run strategy on truly new candles (not on every tick).
        self._last_evaluated_candle_ts: Dict[str, datetime] = {}

        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.feed.resolve_tokens()
        self.feed.bootstrap_history()
        self.feed.start_ticker()
        self.dash.start()
        send_telegram(f"🤖 Trading bot started — {len(config.WATCHLIST)} symbols, "
                      f"{'DRY-RUN' if config.EXECUTION.dry_run else 'LIVE'}")

        self._install_signal_handlers()
        self._running = True
        self._run_loop()

    def shutdown(self, reason: str = "manual") -> None:
        log.info(f"Shutting down: {reason}")
        self._running = False
        # Best-effort: square off everything if we're inside market hours
        if is_trading_window_open() and self.risk.positions:
            log.warning("Squaring off open positions before exit")
            self._squareoff_all("shutdown")
        self.dash.stop()
        send_telegram(f"🛑 Trading bot stopped: {reason}")

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            self.shutdown(f"signal {signum}")
            sys.exit(0)
        # SIGTERM doesn't exist on Windows but signal.signal handles it gracefully
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def _run_loop(self) -> None:
        loop_interval_sec = 1.0
        while self._running:
            loop_start = time.time()
            try:
                self._tick()
            except Exception as e:
                # Never let a single iteration crash the bot
                log.exception(f"Loop iteration failed: {e}")

            # Sleep to maintain ~1 Hz; subtract work done
            elapsed = time.time() - loop_start
            time.sleep(max(0.0, loop_interval_sec - elapsed))

    def _tick(self) -> None:
        # 1) Drain ticks → update candle frames + last_prices
        self.feed.drain_ticks()

        # 2) Forced 15:15 IST square-off (independent of signals)
        if is_squareoff_time() and self.risk.positions:
            self._squareoff_all("daily_squareoff_315pm")

        # 3) Per-symbol: trailing stop + SL/target check (on every tick)
        self._manage_open_positions()

        # 4) Per-symbol: evaluate strategy ONLY when a new candle has closed
        primary = config.TIMING.primary_interval_min
        for sym in self.feed.symbol_to_token:
            df = self.feed.get_candles(sym, primary)
            if df is None or df.empty:
                continue
            latest_ts = df.index[-1].to_pydatetime()
            if self._last_evaluated_candle_ts.get(sym) == latest_ts:
                continue   # already evaluated this candle

            # Compute indicators + evaluate signal
            enriched = ind.compute_all(df)
            snap = ind.latest_snapshot(enriched)
            self.dash.update_indicators(sym, snap)

            has_pos = sym in self.risk.positions
            signal = evaluate(enriched, sym, has_open_position=has_pos)

            if signal.type != SignalType.HOLD:
                self.dash.note_signal(signal)
                log_signal(sym, signal.type.value, signal.price,
                           signal.reason, signal.indicators)
                self._act_on_signal(signal)

            self._last_evaluated_candle_ts[sym] = latest_ts

        # 5) Refresh dashboard
        self.dash.refresh()

    # ------------------------------------------------------------------ #
    # Signal → action
    # ------------------------------------------------------------------ #
    def _act_on_signal(self, sig: Signal) -> None:
        if sig.type == SignalType.BUY:
            self._enter_long(sig)
        elif sig.type == SignalType.EXIT:
            self._exit_position(sig.symbol, sig.price, sig.reason)

    def _enter_long(self, sig: Signal) -> None:
        allowed, deny_reason = self.risk.can_open_new_position(sig.symbol)
        if not allowed:
            log.info(f"BUY {sig.symbol} blocked by risk: {deny_reason}")
            return

        entry = sig.price
        stop, target = self.risk.compute_stop_and_target(entry)
        qty = self.risk.compute_position_size(entry, stop)
        if qty <= 0:
            log.info(f"BUY {sig.symbol} skipped — computed qty = 0")
            return

        result = self.executor.buy(sig.symbol, qty, entry)
        if not result.success:
            log.error(f"BUY order failed for {sig.symbol}: {result.message}")
            log_trade(sig.symbol, "REJECTED", "BUY", qty, entry,
                      reason=result.message)
            return

        # Use the actual fill price for SL/target computation if we got one
        actual_entry = result.fill_price or entry
        if actual_entry != entry:
            stop, target = self.risk.compute_stop_and_target(actual_entry)

        pos = self.risk.open_position(
            sig.symbol, qty, actual_entry, stop, target, result.order_id
        )
        log_trade(sig.symbol, "FILLED", "BUY", qty, actual_entry,
                  order_id=result.order_id, status="COMPLETE",
                  stop_loss=stop, target=target, reason=sig.reason)
        alert_trade(sig.symbol, "BUY", qty, actual_entry, stop, target, sig.reason)

    def _exit_position(self, symbol: str, exit_price: float, reason: str,
                       action_label: str = "EXIT") -> None:
        pos = self.risk.positions.get(symbol)
        if not pos:
            return

        result = self.executor.sell(symbol, pos.quantity, exit_price)
        if not result.success:
            log.error(f"EXIT order failed for {symbol}: {result.message}")
            log_trade(symbol, "REJECTED", "SELL", pos.quantity, exit_price,
                      reason=f"exit_failed: {result.message}")
            return

        actual_exit = result.fill_price or exit_price
        pnl = self.risk.close_position(symbol, actual_exit)
        log_trade(symbol, action_label, "SELL", pos.quantity, actual_exit,
                  order_id=result.order_id, status="COMPLETE",
                  pnl=pnl, reason=reason)
        alert_exit(symbol, pos.quantity, actual_exit, pnl, reason)

    # ------------------------------------------------------------------ #
    # Per-tick risk management for open positions
    # ------------------------------------------------------------------ #
    def _manage_open_positions(self) -> None:
        # Snapshot keys — we may mutate self.risk.positions inside the loop
        for sym in list(self.risk.positions.keys()):
            ltp = self.feed.get_ltp(sym)
            if ltp is None:
                continue

            # Walk trailing stop up if price has moved in our favor
            self.risk.update_trailing_stop(sym, ltp)

            # Did stop or target get hit?
            hit = self.risk.check_stop_or_target_hit(sym, ltp)
            if hit:
                reason = "stop_loss_hit" if hit == "STOP" else "target_hit"
                self._exit_position(sym, ltp, reason, action_label="EXIT")

    def _squareoff_all(self, reason: str) -> None:
        for pos in list(self.risk.positions.values()):
            ltp = self.feed.get_ltp(pos.symbol) or pos.entry_price
            self._exit_position(pos.symbol, ltp, reason, action_label="SQUAREOFF")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main() -> None:
    bot = TradingBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.shutdown("KeyboardInterrupt")
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        bot.shutdown(f"fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
