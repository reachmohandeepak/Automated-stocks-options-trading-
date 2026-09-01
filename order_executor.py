"""
order_executor.py
-----------------
Thin wrapper around the broker order API with:
  * retry-on-transient-failure
  * dry-run mode (logs orders without placing them — KEEP THIS ON during
    development; flip config.EXECUTION.dry_run = False to go live)
  * uniform return shape so main.py doesn't have to know broker quirks

All callers should go through this module — never invoke kite.place_order
directly elsewhere, or the dry-run safety net is bypassed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

import config
from logger import log


@dataclass
class OrderResult:
    success: bool
    order_id: str
    fill_price: float       # broker fill (or LTP used as proxy in dry-run)
    message: str
    is_dry_run: bool = False


class OrderExecutor:
    """Pass an already-authenticated kite client. In dry-run mode the
    kite client is unused — useful for development without API access."""

    def __init__(self, kite_client=None) -> None:
        self.kite = kite_client

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def buy(self, symbol: str, quantity: int, ltp: float,
            order_type: Optional[str] = None) -> OrderResult:
        return self._place(symbol, quantity, "BUY", ltp, order_type)

    def sell(self, symbol: str, quantity: int, ltp: float,
             order_type: Optional[str] = None) -> OrderResult:
        return self._place(symbol, quantity, "SELL", ltp, order_type)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _place(self, symbol: str, quantity: int, side: str, ltp: float,
               order_type: Optional[str]) -> OrderResult:
        if quantity <= 0:
            return OrderResult(False, "", 0.0, "quantity must be > 0")

        otype = (order_type or config.EXECUTION.order_type).upper()

        # Split "NSE:RELIANCE" → exchange + tradingsymbol
        try:
            exchange, tsym = symbol.split(":")
        except ValueError:
            return OrderResult(False, "", 0.0, f"bad symbol format: {symbol}")

        # Compute limit price if needed (LTP ± slippage_paise)
        limit_price = None
        if otype == "LIMIT":
            slip = config.EXECUTION.slippage_paise / 100.0
            limit_price = round(ltp + slip if side == "BUY" else ltp - slip, 2)

        # Dry-run shortcut — no broker call
        if config.EXECUTION.dry_run:
            fake_id = f"DRY-{uuid.uuid4().hex[:8]}"
            fill = limit_price if limit_price is not None else ltp
            log.info(
                f"[DRY-RUN] {side} {symbol} qty={quantity} type={otype} "
                f"@~{fill:.2f} (order_id={fake_id})"
            )
            return OrderResult(True, fake_id, float(fill),
                               "dry_run", is_dry_run=True)

        if self.kite is None:
            return OrderResult(False, "", 0.0,
                               "no broker client in live mode")

        # Live path with retry
        last_err = ""
        for attempt in range(1, config.EXECUTION.max_retries + 1):
            try:
                params = dict(
                    variety=config.EXECUTION.variety,
                    exchange=exchange,
                    tradingsymbol=tsym,
                    transaction_type=side,
                    quantity=int(quantity),
                    product=config.EXECUTION.product,
                    order_type=otype,
                )
                if otype == "LIMIT":
                    params["price"] = limit_price
                elif otype == "MARKET":
                    # Kite requires market_protection on F&O; harmless on equity.
                    params["market_protection"] = getattr(
                        config.EXECUTION, "market_protection_pct", 2.0
                    )

                order_id = self.kite.place_order(**params)
                log.info(f"Order placed: id={order_id} {side} {symbol} "
                         f"qty={quantity} type={otype}")

                # Best-effort fill price lookup (poll order book briefly)
                fill_price = self._wait_for_fill(order_id, fallback=ltp)
                return OrderResult(True, str(order_id), fill_price, "placed")

            except Exception as e:
                last_err = str(e)
                log.warning(
                    f"Order attempt {attempt}/{config.EXECUTION.max_retries} "
                    f"failed for {symbol}: {e}"
                )
                if attempt < config.EXECUTION.max_retries:
                    time.sleep(config.EXECUTION.retry_backoff_sec * attempt)

        return OrderResult(False, "", 0.0, f"all_retries_failed: {last_err}")

    def _wait_for_fill(self, order_id, fallback: float,
                       timeout_sec: float = 3.0) -> float:
        """Poll order history for a fill price. Returns fallback (LTP) if
        not filled in time — caller should treat this as an estimate."""
        if self.kite is None:
            return fallback
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                history = self.kite.order_history(order_id=order_id)
                for h in reversed(history or []):
                    if h.get("status") == "COMPLETE" and h.get("average_price"):
                        return float(h["average_price"])
            except Exception:
                pass
            time.sleep(0.3)
        return fallback
