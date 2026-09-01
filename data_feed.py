"""
data_feed.py
------------
Broker connection + market data pipeline.

Responsibilities:
  1. Authenticate with the broker (Zerodha Kite by default).
  2. Resolve trading-symbol → instrument_token for the watchlist.
  3. Bootstrap historical 1-min and 5-min candles so indicators have
     enough warm-up data on startup.
  4. Subscribe to a live tick websocket and aggregate ticks into rolling
     1-min and 5-min candle DataFrames per symbol.
  5. Expose a thread-safe accessor for those candle frames.

Design notes:
  - The KiteTicker runs on its own thread (twisted reactor). Ticks land in
    a queue; the main loop pops them and updates DataFrames. This keeps
    DataFrame mutation single-threaded.
  - We resample on the fly rather than calling historical_data every minute
    — saves rate limit and gives <1s latency on new candles.
  - Upstox / Angel One stubs are sketched at the bottom. Switching brokers
    is just swapping which class main.py instantiates.
"""

from __future__ import annotations

import threading
import time as time_mod
from collections import defaultdict
from datetime import datetime, timedelta
from queue import Empty, Queue
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytz

import config
from logger import log

IST = pytz.timezone("Asia/Kolkata")


# ============================================================================
# Kite (Zerodha) implementation
# ============================================================================
class KiteDataFeed:
    """Live data + auth wrapper around kiteconnect.KiteConnect + KiteTicker."""

    def __init__(self, symbols: List[str]) -> None:
        try:
            from kiteconnect import KiteConnect, KiteTicker
        except ImportError as e:
            raise ImportError(
                "kiteconnect not installed. Run: pip install kiteconnect"
            ) from e

        self._KiteConnect = KiteConnect
        self._KiteTicker = KiteTicker

        if not config.KITE_API_KEY or not config.KITE_ACCESS_TOKEN:
            raise RuntimeError(
                "KITE_API_KEY and KITE_ACCESS_TOKEN must be set in environment "
                "(see .env.example). Generate access_token daily via the "
                "Kite login URL flow."
            )

        self.kite = KiteConnect(api_key=config.KITE_API_KEY)
        self.kite.set_access_token(config.KITE_ACCESS_TOKEN)

        # symbols formatted as "EXCHANGE:TRADINGSYMBOL"
        self.symbols: List[str] = list(symbols)

        # instrument_token (int)  ↔  "EXCHANGE:TRADINGSYMBOL"
        self.token_to_symbol: Dict[int, str] = {}
        self.symbol_to_token: Dict[str, int] = {}

        # Latest traded price per symbol — updated on every tick
        self.last_prices: Dict[str, float] = {}

        # Per-symbol, per-interval candle DataFrames
        # Outer key = symbol, inner key = interval_minutes (1 or 5)
        self.candles: Dict[str, Dict[int, pd.DataFrame]] = defaultdict(dict)

        # Tick queue: ticker thread → main thread
        self._tick_queue: Queue = Queue(maxsize=10_000)

        self._ticker = None
        self._ticker_thread: Optional[threading.Thread] = None
        self._connected = threading.Event()

    # ------------------------------------------------------------------ #
    # Symbol → token resolution
    # ------------------------------------------------------------------ #
    def resolve_tokens(self) -> None:
        """Fetch the full NSE+BSE instrument dump and map our watchlist
        symbols to their numeric instrument_token (needed by the ticker).
        """
        log.info("Fetching instrument list from Kite...")
        # Combine NSE + BSE dumps; could be filtered to just needed exchanges
        instruments = self.kite.instruments("NSE") + self.kite.instruments("BSE")
        # Build a fast lookup: ("NSE", "RELIANCE") → token
        lookup: Dict[Tuple[str, str], int] = {
            (i["exchange"], i["tradingsymbol"]): i["instrument_token"]
            for i in instruments
        }

        for sym in self.symbols:
            try:
                exch, tsym = sym.split(":")
            except ValueError:
                log.error(f"Bad symbol format (need 'NSE:RELIANCE'): {sym}")
                continue
            token = lookup.get((exch, tsym))
            if not token:
                log.error(f"Could not resolve instrument for {sym}")
                continue
            self.token_to_symbol[token] = sym
            self.symbol_to_token[sym] = token

        log.info(f"Resolved {len(self.symbol_to_token)} / {len(self.symbols)} symbols")

    # ------------------------------------------------------------------ #
    # Historical bootstrap (so indicators have warm-up data)
    # ------------------------------------------------------------------ #
    def bootstrap_history(self) -> None:
        """Pull `bootstrap_candles` worth of recent 1m + 5m candles per
        symbol so EMA50 / RSI / Bollinger are immediately valid."""
        now = datetime.now(IST)
        # Pull a generous window — Kite returns whatever exists
        from_dt = now - timedelta(days=5)

        for sym, token in self.symbol_to_token.items():
            for interval_min in config.TIMING.candle_intervals_min:
                kite_interval = "minute" if interval_min == 1 else f"{interval_min}minute"
                try:
                    data = self.kite.historical_data(
                        instrument_token=token,
                        from_date=from_dt,
                        to_date=now,
                        interval=kite_interval,
                    )
                except Exception as e:
                    log.warning(f"Historical fetch failed for {sym} {kite_interval}: {e}")
                    continue

                if not data:
                    continue

                df = pd.DataFrame(data)
                df["date"] = pd.to_datetime(df["date"]).dt.tz_convert(IST)
                df = df.set_index("date")[["open", "high", "low", "close", "volume"]]
                # Trim to the most recent N
                df = df.tail(config.TIMING.bootstrap_candles)
                self.candles[sym][interval_min] = df
                log.info(f"Bootstrapped {sym} {interval_min}m: {len(df)} candles")
                # Mild throttle to respect rate limits
                time_mod.sleep(0.2)

    # ------------------------------------------------------------------ #
    # WebSocket ticker
    # ------------------------------------------------------------------ #
    def start_ticker(self) -> None:
        """Open the KiteTicker websocket on a background thread."""
        ticker = self._KiteTicker(config.KITE_API_KEY, config.KITE_ACCESS_TOKEN)

        tokens = list(self.token_to_symbol.keys())

        def on_connect(ws, response):
            log.info(f"Ticker connected. Subscribing to {len(tokens)} tokens.")
            ws.subscribe(tokens)
            # MODE_FULL gives OHLC + depth + volume; MODE_LTP is lighter.
            ws.set_mode(ws.MODE_FULL, tokens)
            self._connected.set()

        def on_ticks(ws, ticks):
            # Hot path — don't do any DataFrame work here. Just enqueue.
            for t in ticks:
                try:
                    self._tick_queue.put_nowait(t)
                except Exception:
                    # Queue full — drop oldest by clearing one
                    try:
                        self._tick_queue.get_nowait()
                        self._tick_queue.put_nowait(t)
                    except Empty:
                        pass

        def on_close(ws, code, reason):
            log.warning(f"Ticker closed: {code} {reason}")
            self._connected.clear()

        def on_error(ws, code, reason):
            log.error(f"Ticker error: {code} {reason}")

        def on_reconnect(ws, attempt):
            log.warning(f"Ticker reconnecting (attempt {attempt})")

        ticker.on_connect = on_connect
        ticker.on_ticks = on_ticks
        ticker.on_close = on_close
        ticker.on_error = on_error
        ticker.on_reconnect = on_reconnect

        self._ticker = ticker

        # KiteTicker.connect() is blocking — run it on its own thread.
        # threaded=True makes it non-blocking, but its internal reactor
        # still needs a thread to live on.
        def _run():
            try:
                ticker.connect(threaded=True)
                # Keep this thread alive so KiteTicker's reactor doesn't exit
                while True:
                    time_mod.sleep(1)
            except Exception as e:
                log.error(f"Ticker thread crashed: {e}")

        self._ticker_thread = threading.Thread(
            target=_run, name="KiteTicker", daemon=True
        )
        self._ticker_thread.start()

        # Wait up to 10 seconds for the websocket to connect
        if not self._connected.wait(timeout=10):
            log.warning("Ticker did not signal connect within 10s — continuing anyway")

    # ------------------------------------------------------------------ #
    # Tick → candle aggregation (called from main loop)
    # ------------------------------------------------------------------ #
    def drain_ticks(self) -> None:
        """Pop all queued ticks and fold them into the candle frames.
        Call this from the main loop on every iteration."""
        while True:
            try:
                tick = self._tick_queue.get_nowait()
            except Empty:
                return
            self._apply_tick(tick)

    def _apply_tick(self, tick: dict) -> None:
        token = tick.get("instrument_token")
        sym = self.token_to_symbol.get(token)
        if not sym:
            return

        ltp = tick.get("last_price")
        if ltp is None:
            return
        self.last_prices[sym] = float(ltp)

        # Best-effort volume per tick (Kite gives cumulative day volume in FULL mode)
        # Use traded quantity delta when available; fall back to 0 (price-only update).
        tick_qty = float(tick.get("last_traded_quantity") or 0)

        ts = tick.get("exchange_timestamp") or tick.get("timestamp") or datetime.now(IST)
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = IST.localize(ts)
        elif isinstance(ts, datetime):
            ts = ts.astimezone(IST)

        for interval_min in config.TIMING.candle_intervals_min:
            self._roll_into_candle(sym, interval_min, ts, float(ltp), tick_qty)

    def _roll_into_candle(self, sym: str, interval_min: int,
                          ts: datetime, price: float, qty: float) -> None:
        """Update / create the in-progress candle for this symbol+interval."""
        # Snap timestamp DOWN to the start of its interval bucket
        bucket_min = (ts.minute // interval_min) * interval_min
        candle_ts = ts.replace(minute=bucket_min, second=0, microsecond=0)

        df = self.candles[sym].get(interval_min)
        if df is None or df.empty:
            # Initialize with this tick — happens only if bootstrap was skipped
            df = pd.DataFrame(
                [{"open": price, "high": price, "low": price,
                  "close": price, "volume": qty}],
                index=pd.DatetimeIndex([candle_ts], name="date"),
            )
            self.candles[sym][interval_min] = df
            return

        last_ts = df.index[-1]
        if candle_ts == last_ts:
            # Update existing in-progress candle
            df.at[last_ts, "high"] = max(df.at[last_ts, "high"], price)
            df.at[last_ts, "low"] = min(df.at[last_ts, "low"], price)
            df.at[last_ts, "close"] = price
            df.at[last_ts, "volume"] = df.at[last_ts, "volume"] + qty
        elif candle_ts > last_ts:
            # New candle bucket — append
            new_row = pd.DataFrame(
                [{"open": price, "high": price, "low": price,
                  "close": price, "volume": qty}],
                index=pd.DatetimeIndex([candle_ts], name="date"),
            )
            self.candles[sym][interval_min] = pd.concat([df, new_row])
            # Keep memory bounded — keep last N candles
            cap = max(config.TIMING.bootstrap_candles * 2, 500)
            if len(self.candles[sym][interval_min]) > cap:
                self.candles[sym][interval_min] = self.candles[sym][interval_min].iloc[-cap:]
        # else: tick is older than our latest candle (shouldn't happen) → ignore

    # ------------------------------------------------------------------ #
    # Public accessors
    # ------------------------------------------------------------------ #
    def get_candles(self, symbol: str, interval_min: int) -> Optional[pd.DataFrame]:
        return self.candles.get(symbol, {}).get(interval_min)

    def get_ltp(self, symbol: str) -> Optional[float]:
        return self.last_prices.get(symbol)


# ============================================================================
# Yahoo Finance (no auth, free, 15-min delayed during market hours)
# ============================================================================
class YfinanceDataFeed:
    """Drop-in replacement for KiteDataFeed using yfinance.

    Mirrors KiteDataFeed's public interface (symbol_to_token, last_prices,
    get_candles, get_ltp, drain_ticks, etc.) so main.py doesn't need to
    care which feed is active.

    Limitations vs Kite:
      * No websocket — polls every poll_interval_sec instead.
      * Data is 15-min delayed during NSE market hours (free Yahoo data).
      * Volume on minute bars is sometimes 0 / missing.
      * Only past ~7d of 1-min and ~60d of 5-min data is available.

    Good enough for: validating strategy logic, watching the dashboard,
    running paper trades. NOT suitable for actual live trading.
    """

    def __init__(self, symbols: List[str], poll_interval_sec: int = 60) -> None:
        try:
            import yfinance
        except ImportError as e:
            raise ImportError(
                "yfinance not installed. Run: pip install yfinance"
            ) from e

        self._yf = yfinance
        self.symbols: List[str] = list(symbols)

        # Map our format ("NSE:RELIANCE") to yfinance's ("RELIANCE.NS")
        self.symbol_to_yf: Dict[str, str] = {
            s: self._to_yf_symbol(s) for s in symbols
        }

        # Synthetic tokens so the rest of the code (which uses symbol_to_token)
        # works unchanged. yfinance has no real instrument tokens.
        self.symbol_to_token: Dict[str, int] = {s: i for i, s in enumerate(symbols)}
        self.token_to_symbol: Dict[int, str] = {i: s for s, i in self.symbol_to_token.items()}

        self.last_prices: Dict[str, float] = {}
        self.candles: Dict[str, Dict[int, pd.DataFrame]] = defaultdict(dict)

        # OrderExecutor checks getattr(feed, 'kite', None) — None forces dry-run path
        self.kite = None

        self._poll_interval = poll_interval_sec
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    @staticmethod
    def _to_yf_symbol(sym: str) -> str:
        """NSE:RELIANCE → RELIANCE.NS; BSE:RELIANCE → RELIANCE.BO."""
        if ":" in sym:
            exch, tsym = sym.split(":", 1)
            if exch.upper() == "NSE":
                return f"{tsym}.NS"
            if exch.upper() == "BSE":
                return f"{tsym}.BO"
        return sym

    @staticmethod
    def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
        """Lowercase columns + ensure IST tz + keep only OHLCV."""
        df = df.rename(columns=str.lower)
        keep = ["open", "high", "low", "close", "volume"]
        df = df[[c for c in keep if c in df.columns]]
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(IST)
        else:
            df.index = df.index.tz_convert(IST)
        df.index.name = "date"
        return df

    # ------------------------------------------------------------------ #
    # Public API — mirrors KiteDataFeed
    # ------------------------------------------------------------------ #
    def resolve_tokens(self) -> None:
        """No-op for yfinance — kept for interface compatibility."""
        log.info(f"yfinance feed ready: {len(self.symbol_to_yf)} symbols "
                 f"({list(self.symbol_to_yf.values())})")

    def bootstrap_history(self) -> None:
        """Pull enough recent candles to warm up indicators."""
        for sym, yf_sym in self.symbol_to_yf.items():
            for interval_min in config.TIMING.candle_intervals_min:
                # 1-min data is only available for last ~7 days; 5-min up to 60
                period = "7d" if interval_min == 1 else "60d"
                interval = f"{interval_min}m"
                try:
                    df = self._yf.Ticker(yf_sym).history(
                        period=period, interval=interval, auto_adjust=False
                    )
                except Exception as e:
                    log.warning(f"yfinance bootstrap failed for {sym} {interval}: {e}")
                    continue
                if df is None or df.empty:
                    log.warning(f"yfinance returned empty data for {sym} {interval}")
                    continue
                df = self._normalize_df(df).tail(config.TIMING.bootstrap_candles)
                self.candles[sym][interval_min] = df
                if not df.empty:
                    self.last_prices[sym] = float(df["close"].iloc[-1])
                log.info(f"yfinance bootstrap {sym} {interval}: {len(df)} candles")
                time_mod.sleep(0.3)  # be polite to Yahoo

    def start_ticker(self) -> None:
        """Spin up a background polling thread that refreshes the latest
        candles every poll_interval seconds — yfinance's substitute for
        a websocket."""
        def _poll_loop():
            while not self._stop_event.is_set():
                try:
                    self._poll_once()
                except Exception as e:
                    log.warning(f"yfinance poll error: {e}")
                # Sleep with wakeup on shutdown
                self._stop_event.wait(self._poll_interval)
            log.info("yfinance poll thread exiting")

        self._poll_thread = threading.Thread(
            target=_poll_loop, name="yfinance-poll", daemon=True
        )
        self._poll_thread.start()
        log.info(f"yfinance polling started (every {self._poll_interval}s)")

    def stop(self) -> None:
        """Stop the polling thread cleanly."""
        self._stop_event.set()

    def _poll_once(self) -> None:
        """Fetch the most recent day's worth of candles for each
        symbol/interval and merge into the rolling frames."""
        for sym, yf_sym in self.symbol_to_yf.items():
            for interval_min in config.TIMING.candle_intervals_min:
                try:
                    df = self._yf.Ticker(yf_sym).history(
                        period="1d", interval=f"{interval_min}m", auto_adjust=False
                    )
                except Exception:
                    continue
                if df is None or df.empty:
                    continue
                df = self._normalize_df(df)
                existing = self.candles[sym].get(interval_min)
                if existing is None or existing.empty:
                    self.candles[sym][interval_min] = df
                else:
                    # Replace any overlapping rows (live candle in-progress)
                    # and append new ones
                    merged = pd.concat([existing[~existing.index.isin(df.index)], df])
                    merged = merged.sort_index()
                    cap = max(config.TIMING.bootstrap_candles * 2, 500)
                    self.candles[sym][interval_min] = merged.tail(cap)
                self.last_prices[sym] = float(df["close"].iloc[-1])

    def drain_ticks(self) -> None:
        """No-op — yfinance has no tick stream. Polling thread updates
        candles directly."""
        pass

    def get_candles(self, symbol: str, interval_min: int) -> Optional[pd.DataFrame]:
        return self.candles.get(symbol, {}).get(interval_min)

    def get_ltp(self, symbol: str) -> Optional[float]:
        return self.last_prices.get(symbol)


# ============================================================================
# Stubs — extend if/when you want to swap brokers
# ============================================================================
class UpstoxDataFeed:
    """Stub. Implement using `upstox-python-sdk` if needed."""
    def __init__(self, symbols: List[str]) -> None:
        raise NotImplementedError("Upstox feed not implemented — extend this class")


class AngelDataFeed:
    """Stub. Implement using `smartapi-python` (Angel One SmartAPI)."""
    def __init__(self, symbols: List[str]) -> None:
        raise NotImplementedError("Angel One feed not implemented — extend this class")


# ============================================================================
# Factory
# ============================================================================
def build_feed(symbols: List[str]):
    """Return the right feed implementation per config.BROKER."""
    broker = config.BROKER.lower()
    if broker == "kite":
        return KiteDataFeed(symbols)
    if broker == "yfinance":
        return YfinanceDataFeed(symbols)
    if broker == "upstox":
        return UpstoxDataFeed(symbols)
    if broker == "angel":
        return AngelDataFeed(symbols)
    raise ValueError(f"Unknown broker: {config.BROKER}")
