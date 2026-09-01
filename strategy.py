"""
strategy.py
-----------
Signal generation. Pure logic — no broker calls, no order placement.
Takes an indicator-enriched OHLCV DataFrame and emits a Signal object.

BUY conditions (ALL must hold on the latest closed candle):
  * Price crossed above VWAP within the last `cross_lookback` candles
  * EMA9 > EMA21 > EMA50 (bullish alignment)
  * RSI in [50, 70]
  * MACD line crossed above signal line within the last `cross_lookback` candles

EXIT conditions for an open long (ANY one triggers):
  * Price crossed below VWAP
  * RSI > 75 (overbought)
  * Supertrend flipped bearish
  * Trailing stop hit (checked in risk_manager, not here)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

import config


class SignalType(Enum):
    BUY = "BUY"
    EXIT = "EXIT"     # exit existing long
    HOLD = "HOLD"


@dataclass
class Signal:
    type: SignalType
    symbol: str
    price: float
    reason: str
    # Snapshot of indicators at signal time — useful for logging & alerts
    indicators: dict


# ----------------------------------------------------------------------------
# Helpers — "crossed above" / "crossed below" within a lookback window
# ----------------------------------------------------------------------------
def _crossed_above(series_a: pd.Series, series_b: pd.Series, lookback: int) -> bool:
    """True if `a` was <= `b` somewhere in the last `lookback+1` bars and
    is > `b` on the latest bar. Handles NaNs by treating them as no-cross.
    """
    if len(series_a) < lookback + 2:
        return False
    a = series_a.iloc[-(lookback + 1):].values
    b = series_b.iloc[-(lookback + 1):].values
    if np.isnan(a[-1]) or np.isnan(b[-1]):
        return False
    if a[-1] <= b[-1]:
        return False
    # Any earlier bar in window where a was at/below b → cross occurred
    return any(a[i] <= b[i] for i in range(len(a) - 1) if not np.isnan(a[i]) and not np.isnan(b[i]))


def _crossed_below(series_a: pd.Series, series_b: pd.Series, lookback: int) -> bool:
    if len(series_a) < lookback + 2:
        return False
    a = series_a.iloc[-(lookback + 1):].values
    b = series_b.iloc[-(lookback + 1):].values
    if np.isnan(a[-1]) or np.isnan(b[-1]):
        return False
    if a[-1] >= b[-1]:
        return False
    return any(a[i] >= b[i] for i in range(len(a) - 1) if not np.isnan(a[i]) and not np.isnan(b[i]))


# ----------------------------------------------------------------------------
# Entry signal — BUY (long-only intraday)
# ----------------------------------------------------------------------------
def _check_buy(df: pd.DataFrame, symbol: str) -> Optional[Signal]:
    last = df.iloc[-1]
    close = last["close"]

    # Sanity: any required indicator NaN → not enough data yet
    required = ("vwap", "ema9", "ema21", "ema50", "rsi", "macd", "macd_signal")
    for col in required:
        if pd.isna(last[col]):
            return None

    lookback = config.STRATEGY.cross_lookback

    # 1) Price crossed above VWAP
    if not _crossed_above(df["close"], df["vwap"], lookback):
        return None

    # 2) EMA alignment bullish: 9 > 21 > 50
    if not (last["ema9"] > last["ema21"] > last["ema50"]):
        return None

    # 3) RSI in healthy momentum zone (50–70 by default)
    if not (config.STRATEGY.rsi_buy_min <= last["rsi"] <= config.STRATEGY.rsi_buy_max):
        return None

    # 4) MACD line crossed above signal line recently AND is positive
    if not _crossed_above(df["macd"], df["macd_signal"], lookback):
        return None
    if last["macd"] <= 0:
        return None

    reason = (
        f"VWAP↑cross | EMA9>21>50 | RSI={last['rsi']:.1f} | "
        f"MACD↑cross ({last['macd']:.3f}>{last['macd_signal']:.3f})"
    )

    indicators = {
        "close": float(close),
        "rsi": float(last["rsi"]),
        "macd": float(last["macd"]),
        "macd_signal": float(last["macd_signal"]),
        "ema9": float(last["ema9"]),
        "ema21": float(last["ema21"]),
        "ema50": float(last["ema50"]),
        "vwap": float(last["vwap"]),
        "supertrend_dir": int(last["supertrend_dir"]) if not pd.isna(last["supertrend_dir"]) else 0,
        "pattern": last.get("pattern", ""),
    }
    return Signal(SignalType.BUY, symbol, float(close), reason, indicators)


# ----------------------------------------------------------------------------
# Exit signal — for an open long position
# ----------------------------------------------------------------------------
def _check_exit(df: pd.DataFrame, symbol: str) -> Optional[Signal]:
    last = df.iloc[-1]
    close = float(last["close"])
    reasons = []

    lookback = config.STRATEGY.cross_lookback

    if _crossed_below(df["close"], df["vwap"], lookback):
        reasons.append("price<VWAP")

    if not pd.isna(last["rsi"]) and last["rsi"] > config.STRATEGY.rsi_exit_overbought:
        reasons.append(f"RSI>{config.STRATEGY.rsi_exit_overbought:.0f}")

    if not pd.isna(last["supertrend_dir"]) and last["supertrend_dir"] == -1:
        # Confirm it just flipped (was +1 on a recent bar)
        if len(df) >= 3 and (df["supertrend_dir"].iloc[-3:-1] == 1).any():
            reasons.append("Supertrend↓")

    if not reasons:
        return None

    snapshot = {
        "close": close,
        "rsi": float(last["rsi"]) if not pd.isna(last["rsi"]) else None,
        "vwap": float(last["vwap"]) if not pd.isna(last["vwap"]) else None,
        "supertrend_dir": int(last["supertrend_dir"]) if not pd.isna(last["supertrend_dir"]) else 0,
    }
    return Signal(SignalType.EXIT, symbol, close, " + ".join(reasons), snapshot)


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
def evaluate(df: pd.DataFrame, symbol: str, has_open_position: bool) -> Signal:
    """Evaluate strategy on the latest candle. Returns a Signal object.
    Always returns something (never None) — HOLD if no action."""
    if df is None or df.empty or len(df) < 50:
        # EMA50 needs 50 bars to be meaningful
        return Signal(SignalType.HOLD, symbol, 0.0, "warming_up", {})

    if has_open_position:
        exit_sig = _check_exit(df, symbol)
        if exit_sig:
            return exit_sig
        return Signal(SignalType.HOLD, symbol, float(df.iloc[-1]["close"]),
                      "in_position", {})

    buy_sig = _check_buy(df, symbol)
    if buy_sig:
        return buy_sig
    return Signal(SignalType.HOLD, symbol, float(df.iloc[-1]["close"]),
                  "no_setup", {})
