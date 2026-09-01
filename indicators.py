"""
indicators.py
-------------
Pure functions that take an OHLCV pandas DataFrame and return it enriched
with indicator columns. No I/O, no broker calls — easy to unit-test.

Required input DataFrame schema:
  index : pandas.DatetimeIndex (tz-aware, IST)
  cols  : ['open', 'high', 'low', 'close', 'volume']

All indicators are implemented in pure pandas/numpy — no external TA
library (pandas-ta would pull in numba which doesn't support Python 3.14).
Implementations follow the standard definitions used by TradingView /
most retail charting platforms.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config


# ============================================================================
# Moving averages
# ============================================================================
def _ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average — uses adjust=False to match TradingView."""
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    df["ema9"] = _ema(df["close"], config.INDICATORS.ema_short)
    df["ema21"] = _ema(df["close"], config.INDICATORS.ema_mid)
    df["ema50"] = _ema(df["close"], config.INDICATORS.ema_long)
    return df


# ============================================================================
# RSI — Wilder's smoothing
# ============================================================================
def add_rsi(df: pd.DataFrame) -> pd.DataFrame:
    length = config.INDICATORS.rsi_length
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    # Wilder's smoothing = EMA with alpha=1/length (equivalent to ewm(com=length-1))
    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    # Avoid divide-by-zero: where avg_loss == 0, RSI = 100
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100.0 - (100.0 / (1.0 + rs))
    # Clean up: when avg_loss was zero AND avg_gain > 0, force RSI to 100
    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100.0
    df.loc[(avg_gain == 0) & (avg_loss == 0), "rsi"] = 50.0
    return df


# ============================================================================
# MACD — fast EMA - slow EMA, signal = EMA of MACD
# ============================================================================
def add_macd(df: pd.DataFrame) -> pd.DataFrame:
    fast = config.INDICATORS.macd_fast
    slow = config.INDICATORS.macd_slow
    sig = config.INDICATORS.macd_signal

    ema_fast = _ema(df["close"], fast)
    ema_slow = _ema(df["close"], slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, sig)

    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"] = macd_line - signal_line
    return df


# ============================================================================
# Bollinger Bands — SMA ± k * stdev
# ============================================================================
def add_bollinger(df: pd.DataFrame) -> pd.DataFrame:
    length = config.INDICATORS.bb_length
    k = config.INDICATORS.bb_std
    mid = df["close"].rolling(length, min_periods=length).mean()
    # Use population std (ddof=0) to match TradingView's default
    std = df["close"].rolling(length, min_periods=length).std(ddof=0)
    df["bb_mid"] = mid
    df["bb_upper"] = mid + k * std
    df["bb_lower"] = mid - k * std
    return df


# ============================================================================
# Session-anchored VWAP
# ============================================================================
def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Anchored VWAP — resets at the start of every trading session (date).
    Computed as cumulative(typical_price * volume) / cumulative(volume),
    where typical_price = (H + L + C) / 3.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tpv = typical * df["volume"]

    # Group by the IST trading date — index is assumed tz-aware (IST)
    session = df.index.date
    df = df.assign(_tpv=tpv, _vol=df["volume"], _session=session)
    df["vwap"] = (
        df.groupby("_session")["_tpv"].cumsum()
        / df.groupby("_session")["_vol"].cumsum().replace(0, np.nan)
    )
    df = df.drop(columns=["_tpv", "_vol", "_session"])
    return df


# ============================================================================
# ADX — trend strength filter (Wilder 1978)
# ============================================================================
def add_adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """Average Directional Index — measures TREND STRENGTH (not direction).
    ADX > 25 = trending. ADX < 20 = choppy / range-bound. Used to FILTER OUT
    sideways markets where breakout strategies get whipsawed.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Directional Movements
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move

    # Wilder's smoothed averages (EMA with alpha=1/length)
    atr = tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False, min_periods=length).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False, min_periods=length).mean() / atr.replace(0, np.nan)

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    df["adx"] = dx.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di
    return df


# ============================================================================
# Volume average — for participation/confirmation filter
# ============================================================================
def add_volume_features(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    """Adds rolling average volume + a 'volume_ratio' (current / avg).
    volume_ratio > 1.5 = above-average participation."""
    df["vol_avg20"] = df["volume"].rolling(length, min_periods=length).mean()
    df["volume_ratio"] = df["volume"] / df["vol_avg20"].replace(0, np.nan)
    return df


# ============================================================================
# Supertrend — ATR-based trend-follower
# ============================================================================
def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    """Average True Range using Wilder's smoothing."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def add_supertrend(df: pd.DataFrame) -> pd.DataFrame:
    """Classic Supertrend.

    Direction convention (matches pandas-ta): +1 = bullish (price > ST line),
    -1 = bearish (price < ST line).
    """
    length = config.INDICATORS.supertrend_length
    mult = config.INDICATORS.supertrend_multiplier

    atr = _atr(df, length)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper_basic = hl2 + mult * atr
    lower_basic = hl2 - mult * atr

    n = len(df)
    upper = upper_basic.copy()
    lower = lower_basic.copy()
    supertrend = pd.Series(np.nan, index=df.index)
    direction = pd.Series(0, index=df.index, dtype=int)

    close = df["close"].values
    ub = upper_basic.values.copy()
    lb = lower_basic.values.copy()
    st = np.full(n, np.nan)
    dir_arr = np.zeros(n, dtype=int)

    # Initialize from first non-NaN ATR row
    start = int(atr.notna().idxmax().value) if atr.notna().any() else None
    if start is None:
        # Not enough data — return NaN columns
        df["supertrend"] = supertrend
        df["supertrend_dir"] = direction
        return df

    # Find positional index of first non-NaN
    first_valid = atr.reset_index(drop=True).first_valid_index()
    if first_valid is None:
        df["supertrend"] = supertrend
        df["supertrend_dir"] = direction
        return df

    # Seed: assume bullish at first valid bar
    dir_arr[first_valid] = 1
    st[first_valid] = lb[first_valid]

    for i in range(first_valid + 1, n):
        # "Final" upper / lower bands — they only widen against trend
        if ub[i] < ub[i - 1] or close[i - 1] > ub[i - 1]:
            pass  # keep ub[i]
        else:
            ub[i] = ub[i - 1]

        if lb[i] > lb[i - 1] or close[i - 1] < lb[i - 1]:
            pass  # keep lb[i]
        else:
            lb[i] = lb[i - 1]

        prev_dir = dir_arr[i - 1]
        if prev_dir == 1:
            # Previously bullish — flip to bearish only if close breaks below lower band
            if close[i] < lb[i]:
                dir_arr[i] = -1
                st[i] = ub[i]
            else:
                dir_arr[i] = 1
                st[i] = lb[i]
        else:
            # Previously bearish — flip to bullish only if close breaks above upper band
            if close[i] > ub[i]:
                dir_arr[i] = 1
                st[i] = lb[i]
            else:
                dir_arr[i] = -1
                st[i] = ub[i]

    df["supertrend"] = pd.Series(st, index=df.index)
    df["supertrend_dir"] = pd.Series(dir_arr, index=df.index)
    return df


# ============================================================================
# Candlestick patterns (Engulfing, Doji, Hammer)
# ============================================================================
def _body(o: float, c: float) -> float:
    return abs(c - o)


def _candle_range(h: float, l: float) -> float:
    return max(h - l, 1e-9)  # guard against zero-range candles


def detect_doji(o: float, h: float, l: float, c: float) -> int:
    """Body is <= 10% of total range — indecision candle."""
    return 1 if _body(o, c) <= 0.10 * _candle_range(h, l) else 0


def detect_hammer(o: float, h: float, l: float, c: float) -> int:
    """Small body near the top, long lower wick (>= 2x body), tiny upper wick."""
    rng = _candle_range(h, l)
    body = _body(o, c)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if body == 0:
        return 0
    if (lower_wick >= 2 * body
            and upper_wick <= 0.3 * body
            and body <= 0.4 * rng):
        return 1
    return 0


def detect_engulfing(prev_o: float, prev_c: float,
                     o: float, c: float) -> int:
    """+1 bullish engulfing, -1 bearish engulfing, 0 none."""
    prev_red = prev_c < prev_o
    prev_green = prev_c > prev_o
    cur_green = c > o
    cur_red = c < o

    if prev_red and cur_green and c >= prev_o and o <= prev_c:
        return 1
    if prev_green and cur_red and o >= prev_c and c <= prev_o:
        return -1
    return 0


def add_candlestick_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Append `pattern` column. Priority: engulfing > hammer > doji."""
    patterns = []
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values

    for i in range(len(df)):
        label = ""
        if i > 0:
            e = detect_engulfing(o[i - 1], c[i - 1], o[i], c[i])
            if e == 1:
                label = "bullish_engulfing"
            elif e == -1:
                label = "bearish_engulfing"
        if not label and detect_hammer(o[i], h[i], l[i], c[i]):
            label = "hammer"
        if not label and detect_doji(o[i], h[i], l[i], c[i]):
            label = "doji"
        patterns.append(label)

    df["pattern"] = patterns
    return df


# ============================================================================
# Pipeline — compute all indicators in one call
# ============================================================================
def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Run the full indicator stack on an OHLCV DataFrame.

    Returns the same frame with added columns. Caller should ensure the
    frame has at least `ema_long` rows (50) for EMA50 to be meaningful;
    otherwise the latest values will be NaN.
    """
    if df.empty:
        return df

    # Defensive copy — never mutate caller's frame
    df = df.copy()

    df = add_emas(df)
    df = add_rsi(df)
    df = add_macd(df)
    df = add_bollinger(df)
    df = add_vwap(df)
    df = add_supertrend(df)
    df = add_adx(df)
    df = add_volume_features(df)
    df = add_candlestick_patterns(df)
    return df


# ============================================================================
# Convenience: extract latest row as a flat dict for logging/dashboard
# ============================================================================
def latest_snapshot(df: pd.DataFrame) -> dict:
    """Return the most recent indicator values as a dict (NaN-safe)."""
    if df.empty:
        return {}
    last = df.iloc[-1]
    out = {}
    for col in ("close", "rsi", "macd", "macd_signal", "macd_hist",
                "ema9", "ema21", "ema50", "vwap",
                "bb_upper", "bb_lower", "supertrend", "supertrend_dir",
                "pattern"):
        if col in last.index:
            v = last[col]
            if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                continue
            out[col] = v
    return out
