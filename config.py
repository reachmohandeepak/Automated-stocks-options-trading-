"""
config.py
---------
Central configuration for the intraday trading bot. All user-tunable
parameters live here so other modules stay free of magic numbers.

Secrets are loaded from environment variables (or a `.env` file) — never
hard-code API keys or access tokens.
"""

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

# Load `.env` file into os.environ if present
load_dotenv()


# ----------------------------------------------------------------------------
# Broker selection
# ----------------------------------------------------------------------------
# Supported: "kite" (Zerodha, live), "yfinance" (free, 15-min delayed,
# no auth needed — good for testing). Stubs for "upstox" and "angel".
BROKER: str = "kite"

# ----------------------------------------------------------------------------
# API credentials (loaded from environment)
# ----------------------------------------------------------------------------
KITE_API_KEY: str = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET: str = os.getenv("KITE_API_SECRET", "")
KITE_ACCESS_TOKEN: str = os.getenv("KITE_ACCESS_TOKEN", "")

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")


# ----------------------------------------------------------------------------
# Watchlist
# ----------------------------------------------------------------------------
# Format: "EXCHANGE:TRADINGSYMBOL". Kite uses NSE / BSE prefixes.
# Keep the list small (5–15 names) — too many parallel subscriptions stress
# the websocket and indicator pipeline.
WATCHLIST: List[str] = [
    # Kept (positive or near-breakeven in v1 backtest):
    "NSE:RELIANCE",
    "NSE:TCS",
    "NSE:ICICIBANK",
    "NSE:SBIN",
    # Removed (poor performers in 60-day v1 backtest):
    #   "NSE:INFY"     — 16 trades, 12.5% wins, -3.11%
    #   "NSE:HDFCBANK" — 7 trades,  0%   wins, -2.54%
    # Added (high-beta trenders, popular intraday names):
    "NSE:BAJFINANCE",
    "NSE:AXISBANK",
    "NSE:TATAMOTORS",
    "NSE:BHARTIARTL",
]


# ----------------------------------------------------------------------------
# Capital & risk
# ----------------------------------------------------------------------------
@dataclass
class RiskConfig:
    # Total trading capital (INR). Used for position sizing & daily loss cap.
    capital: float = 100_000.0

    # Risk per trade as a fraction of capital (1–2% is the standard rule).
    risk_per_trade_pct: float = 0.01  # 1%

    # Stop-loss distance as a fraction of entry price (0.5% – 1%).
    stop_loss_pct: float = 0.007  # 0.7%

    # Reward-to-risk multiple. Target distance = stop_loss_distance * this.
    # Spec requires minimum 1:1.5; 2.0 gives a healthier edge.
    reward_to_risk: float = 2.0

    # Trailing stop activates once price has moved this fraction in our favor.
    # The trail then follows price by the original stop-loss distance.
    trail_activate_pct: float = 0.005  # 0.5%

    # Hard daily loss limit — if breached, halt all new entries for the day.
    daily_loss_limit_pct: float = 0.03  # 3% of capital

    # Maximum simultaneous open positions (caps concentration risk).
    max_open_positions: int = 3


RISK = RiskConfig()


# ----------------------------------------------------------------------------
# Indicator parameters (matches the spec)
# ----------------------------------------------------------------------------
@dataclass
class IndicatorConfig:
    rsi_length: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_length: int = 20
    bb_std: float = 2.0
    ema_short: int = 9
    ema_mid: int = 21
    ema_long: int = 50
    supertrend_length: int = 10
    supertrend_multiplier: float = 3.0


INDICATORS = IndicatorConfig()


# ----------------------------------------------------------------------------
# Strategy thresholds
# ----------------------------------------------------------------------------
@dataclass
class StrategyConfig:
    rsi_buy_min: float = 50.0
    rsi_buy_max: float = 70.0
    rsi_exit_overbought: float = 75.0
    # How many candles back to look for "price crossed VWAP" / MACD cross.
    cross_lookback: int = 2


STRATEGY = StrategyConfig()


# ----------------------------------------------------------------------------
# Timing — all times are India Standard Time (IST, UTC+5:30)
# ----------------------------------------------------------------------------
@dataclass
class TimingConfig:
    market_open_hhmm: tuple = (9, 15)     # 09:15 IST
    market_close_hhmm: tuple = (15, 30)   # 15:30 IST
    # Auto square-off ALL open positions at this time. Spec mandates 15:15.
    squareoff_hhmm: tuple = (15, 15)
    # No new entries within this many minutes of square-off (avoid getting
    # trapped right before forced exit).
    no_new_entries_before_close_min: int = 20
    # Candle intervals to compute on (minutes). Strategy uses 5-min primary.
    candle_intervals_min: List[int] = field(default_factory=lambda: [1, 5])
    primary_interval_min: int = 5
    # How many historical candles to bootstrap on startup (need >= 50 for EMA50).
    bootstrap_candles: int = 200


TIMING = TimingConfig()


# ----------------------------------------------------------------------------
# Order execution
# ----------------------------------------------------------------------------
@dataclass
class ExecutionConfig:
    # "MARKET" or "LIMIT". Limit uses LTP +/- slippage_paise.
    order_type: str = "MARKET"
    product: str = "MIS"            # Intraday — auto-squareoff by broker too
    variety: str = "regular"
    exchange_default: str = "NSE"
    slippage_paise: int = 5         # For LIMIT orders only
    # Retry on transient API failures
    max_retries: int = 3
    retry_backoff_sec: float = 1.5
    # Dry-run: log orders but do not actually place them. Flip to False to go live.
    dry_run: bool = True
    # Kite requires market_protection on MARKET orders for F&O (% slippage cap).
    # 2% is conservative for equity; options bot uses 5% in its own config.
    market_protection_pct: float = 2.0


EXECUTION = ExecutionConfig()


# ----------------------------------------------------------------------------
# Logging / output paths
# ----------------------------------------------------------------------------
LOG_DIR: str = "logs"
TRADE_LOG_CSV: str = os.path.join(LOG_DIR, "trades.csv")
SIGNAL_LOG_CSV: str = os.path.join(LOG_DIR, "signals.csv")
APP_LOG_FILE: str = os.path.join(LOG_DIR, "app.log")
