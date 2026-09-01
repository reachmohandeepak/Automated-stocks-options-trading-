"""
options_bot.py
--------------
Fully automated options trading bot for NIFTY / BANKNIFTY / SENSEX.

What it does:
  * Polls each underlying every 5 min for a directional signal
    (reuses options_signal.generate_signal — 4-of-5 confirmation).
  * Resolves the live ATM CE/PE contract via Kite instruments dump.
  * Fetches live option premium via kite.quote().
  * Sizes position in LOTS based on risk budget vs spot stop distance.
  * Places real BUY orders via Kite (or dry-run logs).
  * Polls live spot every 30s — exits on stop_spot, target_spot, or expiry-day cut-off.
  * Daily loss kill switch (3% of capital → halt new entries).
  * 14:30 forced exit on expiry day; 15:15 daily square-off.
  * Telegram alert on every event.

Modes:
  --auto-orders off (DEFAULT, SAFE)
        Sends Telegram BUY/EXIT signal messages only. No real orders.
        Good for: validating the strategy, watching signals, learning.

  --auto-orders on
        Places REAL orders via Kite. Money will move from your Zerodha
        account. Position sized to your LIVE available cash (fetched
        from kite.margins() at startup) unless --capital overrides it.

Run examples:
  python options_bot.py                          # signals-only (safe)
  python options_bot.py --auto-orders off        # same as default
  python options_bot.py --auto-orders on         # REAL ORDERS
  python options_bot.py --auto-orders on --capital 25000   # override balance

Stop: Ctrl+C (squares off open positions on exit)
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import signal as signal_mod
import sys
import time

# Force UTF-8 on Windows console so log lines with ₹ / — don't trip cp1252
for stream_name in ("stdout", "stderr"):
    try:
        getattr(sys, stream_name).reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, Optional

import pandas as pd
import pytz
from kiteconnect import KiteConnect, exceptions as kc_exc

import config
from logger import log, send_telegram
from options_signal import UNDERLYINGS, Underlying, generate_signal, OptionsSignal

IST = pytz.timezone("Asia/Kolkata")


# ============================================================================
# Options-specific configuration (separate from stock bot's config)
# ============================================================================
@dataclass
class OptionsRiskConfig:
    capital: float = 50_000.0
    risk_per_trade_pct: float = 0.01          # 1% → ₹500 max loss per trade
    daily_loss_limit_pct: float = 0.03        # 3% → halt at -₹1500
    max_open_positions: int = 2               # max concurrent options trades
    # Force exit time on expiry day (theta + erratic moves in last hour)
    expiry_day_exit_hhmm: tuple = (15, 15)
    # Daily square-off for non-expiry days (matches stock bot)
    daily_squareoff_hhmm: tuple = (15, 15)
    # Observe the market for this many minutes after open before taking ANY new
    # entry (e.g. 60 = no entries until 10:15 on a 09:15 open). The first hour is
    # the most whipsaw-prone / widest-spread window. 0 = trade from the open.
    skip_open_minutes: int = 0
    # Last time of day a NEW entry may be opened. No new buys after this — the
    # closing window bleeds theta and leaves no time for the move to work before
    # square-off. Exits still run after this. (None disables the cutoff.)
    last_entry_hhmm: tuple = (14, 45)
    # ----- Small-capital mode overrides (activated by --small-capital flag) -----
    small_capital_mode: bool = False
    # When in small mode: trade only this underlying (cheapest lot)
    small_underlyings: tuple = ("SENSEX",)
    # Catastrophic premium stop — exit if premium drops by this fraction.
    # Even in "no SL" mode this is the floor that prevents -100% on a single trade.
    catastrophic_premium_stop_pct: float = 0.60   # exit at -60% premium
    # In small mode, ignore the 1%-risk math, always buy 1 lot.
    force_one_lot: bool = False
    # Optional ceiling on lots per trade. 0 = AUTO-size from capital + live lot
    # price (small-capital buys as many lots as the per-trade budget affords;
    # risk-based mode uncapped). >0 = cap the auto figure at this many lots.
    max_lots: int = 0
    # Max budget per trade in small mode (₹). 0 = no cap.
    max_premium_cost_per_trade: float = 0.0
    # Max trades per day — prevents revenge trading. 0 = no cap.
    # In small-capital mode, defaults to 3 (set in main()).
    max_trades_per_day: int = 0
    # Cooldown in seconds after an exit before re-entering the SAME underlying.
    # Prevents the "exit-and-immediate-reentry" pattern that burns fees on whipsaws.
    # Default 1800s (30 min). Set 0 to disable.
    cooldown_sec_after_exit: int = 1800
    # Extended cooldown applied only after a LOSING exit. Takes max of this and
    # cooldown_sec_after_exit. Default 1800s. Rationale: losses cluster in chop
    # — a longer lockout after a loss reduces revenge-trade frequency without
    # penalising winning streaks.
    cooldown_sec_after_loss: int = 1800
    # Range/chop filter — ADX < this threshold means market is choppy/range-bound.
    # Block new entries below this value. ADX < 20 is the standard "no trend" zone;
    # 22-25 is more conservative. Set 0 to disable.
    adx_min_threshold: float = 20.0
    # Minimum confirmation score required to enter. 0 = use the default threshold
    # (3 for indices, since VWAP is unavailable so max score is 4). Set 4 to demand
    # ALL available confirmations (incl. MACD) — skips marginal 3/5 setups.
    min_score: int = 0
    # Stricter RSI confluence — require RSI further from 50 to count as a
    # bullish/bearish confirmation. Original 50<rsi<70 and 30<rsi<50 picked up
    # borderline neutral readings that whipsawed. Tighter window forces real
    # momentum before counting it as a confirmation.
    rsi_bull_min: float = 60.0   # was effectively 50
    rsi_bull_max: float = 70.0
    rsi_bear_min: float = 30.0
    rsi_bear_max: float = 40.0   # was effectively 50
    # Refuse entry if bid-ask spread exceeds this fraction of mid-price.
    # Wide spread = illiquid OTM strike where slippage eats profits.
    max_bid_ask_spread_pct: float = 0.02   # 2%
    # Refuse entry if computed stop is closer than this to current spot.
    # Catches the "stop already breached at entry" bug class.
    min_stop_distance_pct: float = 0.003   # 0.3% minimum
    # Refuse entry if premium has spiked >this% from today's open.
    # Avoids buying at the day's high (a common retail mistake).
    max_premium_above_open_pct: float = 0.30   # 30% above today's open
    # Quick-profit rule: exit when realized unrealized gain reaches this Rs amount.
    # Set 0 to disable. Validated in backtest (60-day SENSEX, 1 lot):
    #   Rs 300 target → 84.8% win rate, +Rs 41,470 modeled net P&L
    quick_profit_target_rs: float = 300.0
    # Trailing quick-profit = RATCHET LOCK. Once profit reaches the target, the
    # bot does NOT exit immediately — it lets profit ride and locks in escalating
    # floors in steps of this size. The locked floor only ever rises (a ratchet),
    # so a sudden collapse can never erase a banked gain or go negative.
    #   locked_floor = target + floor((peak - target) / step) * step
    # e.g. target 400, step 50: peak +968 -> locked floor +950 (max giveback ≈ one
    # step). Smaller step = tighter lock (less giveback, exits sooner on noise);
    # larger step = more room to run. Set 0 to exit immediately when target hits.
    quick_profit_trail_rs: float = 50.0
    # Quick-loss rule: exit when unrealized LOSS reaches this Rs amount.
    # Tight per-trade stop in absolute Rs (e.g. 150 = exit at -Rs 150).
    # Fires BEFORE spot stop or catastrophic premium stop. Set 0 to disable.
    quick_loss_limit_rs: float = 150.0
    # Daily profit target — halt new entries once cumulative realized P&L >= this.
    # Locks in good days. Set 0 to disable (default).
    daily_profit_target_rs: float = 0.0
    # Daily loss limit in absolute Rs (alternative/override to daily_loss_limit_pct).
    # If both set, daily_loss_limit_rs takes priority. Set 0 to use the pct value.
    daily_loss_limit_rs: float = 0.0


@dataclass
class OptionsExecutionConfig:
    dry_run: bool = True             # ← KEEP TRUE until validated for full session
    product: str = "MIS"             # intraday — broker auto-squareoff too
    order_type: str = "MARKET"
    variety: str = "regular"
    max_retries: int = 3
    retry_backoff_sec: float = 1.5
    # Kite now requires market_protection on MARKET orders for F&O — caps the
    # max slippage from current LTP. Options move fast, so 5% is reasonable.
    # If you'd rather guarantee a price, switch order_type to "LIMIT".
    market_protection_pct: float = 5.0


OPTIONS_RISK = OptionsRiskConfig()
OPTIONS_EXECUTION = OptionsExecutionConfig()

POLL_SIGNAL_SEC = 300     # regenerate signals every 5 min
POLL_TICK_SEC = 30        # idle loop cadence when flat (no open position)
POLL_EXIT_SEC = 5         # fast cadence while holding — catches premium spikes/
                          # collapses so the ratchet floor isn't skipped over
OPTIONS_LOG_CSV = os.path.join(config.LOG_DIR, "options_orders.csv")

# Exchange routing: SENSEX/BANKEX trade on BFO; NIFTY/BANKNIFTY on NFO
EXCHANGE_FOR = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "SENSEX": "BFO",
    "BANKEX": "BFO",
}


# ============================================================================
# Kite client (single shared instance)
# ============================================================================
def make_kite() -> KiteConnect:
    if not config.KITE_API_KEY or not config.KITE_ACCESS_TOKEN:
        raise RuntimeError("KITE_API_KEY and KITE_ACCESS_TOKEN must be set in .env")
    k = KiteConnect(api_key=config.KITE_API_KEY)
    k.set_access_token(config.KITE_ACCESS_TOKEN)
    # Fail fast if auth is bad
    k.profile()
    return k


def fetch_available_cash(kite: KiteConnect) -> Optional[float]:
    """Query Kite for live available cash in the equity segment (which
    covers F&O margin too). Returns rupees or None on failure.

    Tries `live_balance` first (includes intraday P&L), then `cash`.
    """
    try:
        margins = kite.margins(segment="equity")
    except TypeError:
        # Older SDK signature returns all segments without arg
        margins = kite.margins().get("equity", {})
    except Exception as e:
        log.warning(f"Could not fetch margins: {e}")
        return None

    if not margins:
        return None
    avail = margins.get("available", {}) if isinstance(margins, dict) else {}
    for key in ("live_balance", "cash", "net"):
        v = avail.get(key)
        if v is not None:
            return float(v)
    return None


# ============================================================================
# Real-time index data — replaces yfinance (which is 15-min delayed)
# ============================================================================
# Maps our underlying names to (exchange, tradingsymbol) on Kite's index list
INDEX_SYMBOLS = {
    "NIFTY":     ("NSE", "NIFTY 50"),
    "BANKNIFTY": ("NSE", "NIFTY BANK"),
    "SENSEX":    ("BSE", "SENSEX"),
    "FINNIFTY":  ("NSE", "NIFTY FIN SERVICE"),
}


class IndexDataFetcher:
    """Pulls live index spot + historical candles via Kite (real-time, not
    15-min delayed like yfinance)."""

    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self._tokens: Dict[str, int] = {}

    def _resolve_token(self, name: str) -> Optional[int]:
        if name in self._tokens:
            return self._tokens[name]
        if name not in INDEX_SYMBOLS:
            log.error(f"Unknown index: {name}")
            return None
        exch, tsym = INDEX_SYMBOLS[name]
        try:
            dump = self.kite.instruments(exch)
        except Exception as e:
            log.warning(f"Could not fetch {exch} instruments: {e}")
            return None
        for i in dump:
            if i.get("tradingsymbol") == tsym:
                self._tokens[name] = int(i["instrument_token"])
                log.info(f"Index token resolved: {name} -> {self._tokens[name]}")
                return self._tokens[name]
        log.warning(f"Token not found for {name} on {exch}")
        return None

    def fetch_candles(self, name: str, days: int = 5,
                       interval: str = "5minute") -> Optional[pd.DataFrame]:
        """Returns OHLCV DataFrame indexed by IST datetime. Real-time, not delayed."""
        token = self._resolve_token(name)
        if not token:
            return None
        from datetime import datetime, timedelta
        to_dt = datetime.now(IST)
        from_dt = to_dt - timedelta(days=days)
        try:
            data = self.kite.historical_data(
                instrument_token=token, from_date=from_dt,
                to_date=to_dt, interval=interval,
            )
        except Exception as e:
            log.warning(f"historical_data failed for {name}: {e}")
            return None
        if not data:
            return None
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        if df["date"].dt.tz is None:
            df["date"] = df["date"].dt.tz_localize("UTC").dt.tz_convert(IST)
        else:
            df["date"] = df["date"].dt.tz_convert(IST)
        df = df.set_index("date")[["open", "high", "low", "close", "volume"]]
        return df


# ============================================================================
# Real-time scoring — same 4-of-5 logic as options_signal.py, but on a
# pre-computed DataFrame (so we can reuse it with Kite data).
# ============================================================================
import pandas as _pd
import indicators as _ind


@dataclass
class LiveSignal:
    """Outcome of scoring on real-time Kite data."""
    direction: str          # "BUY_CALL" | "BUY_PUT" | "NO_TRADE"
    spot: float             # real-time, not delayed
    stop_spot: float
    target_spot: float
    reason: str
    score: int


def score_realtime(df: _pd.DataFrame,
                    stop_pct: float = 0.004,
                    target_pct: float = 0.008,
                    rsi_bull_min: float = 60.0,
                    rsi_bull_max: float = 70.0,
                    rsi_bear_min: float = 30.0,
                    rsi_bear_max: float = 40.0,
                    adx_min: float = 20.0,
                    min_score: int = 0) -> LiveSignal:
    """Score the most recent bar in df. df must have enough warmup (>= 60 bars).
    Returns LiveSignal — stop/target levels are computed from the LATEST bar's
    close (real-time spot).

    RSI band defaults are tighter than the original 50/70-30/50 to require real
    momentum (not neutral readings). ADX filter rejects choppy markets even when
    the confluence score is met — without it the bot whipsaws in range-bound tape.
    """
    if df is None or len(df) < 60:
        return LiveSignal("NO_TRADE", 0, 0, 0, "warmup", 0)

    enriched = _ind.compute_all(df)
    last = enriched.iloc[-1]
    spot = float(last["close"])

    required = ("ema9", "ema21", "ema50", "rsi", "macd", "macd_signal",
                "supertrend_dir")
    if any(_pd.isna(last[c]) for c in required):
        return LiveSignal("NO_TRADE", spot, 0, 0, "indicators_warmup", 0)

    bull, bear = 0, 0
    bull_r, bear_r = [], []

    # 1. VWAP (skip if NaN — common on indices)
    vwap_present = (not _pd.isna(last.get("vwap"))) and last["vwap"] > 0
    if vwap_present:
        if spot > last["vwap"]:
            bull += 1; bull_r.append("spot>VWAP")
        elif spot < last["vwap"]:
            bear += 1; bear_r.append("spot<VWAP")

    # 2. EMA alignment
    # NOTE: use safe-for-HTML chars (Telegram parses HTML and breaks on raw <,>)
    e9, e21, e50 = last["ema9"], last["ema21"], last["ema50"]
    if e9 > e21 > e50:
        bull += 1; bull_r.append("EMA-bull")
    elif e9 < e21 < e50:
        bear += 1; bear_r.append("EMA-bear")

    # 3. RSI — tighter band; readings near 50 no longer count as confirmation
    rsi = last["rsi"]
    if rsi_bull_min < rsi < rsi_bull_max:
        bull += 1; bull_r.append(f"RSI{rsi:.0f}")
    elif rsi_bear_min < rsi < rsi_bear_max:
        bear += 1; bear_r.append(f"RSI{rsi:.0f}")

    # 4. MACD
    if last["macd"] > last["macd_signal"] and last["macd"] > 0:
        bull += 1; bull_r.append("MACD-up")
    elif last["macd"] < last["macd_signal"] and last["macd"] < 0:
        bear += 1; bear_r.append("MACD-down")

    # 5. Supertrend
    if last["supertrend_dir"] == 1:
        bull += 1; bull_r.append("ST-bull")
    elif last["supertrend_dir"] == -1:
        bear += 1; bear_r.append("ST-bear")

    base_threshold = 4 if vwap_present else 3
    # --min-score raises the bar (require more confirmations). For indices VWAP
    # is absent so max score is 4; min_score=4 means "all available confirmations".
    threshold = max(base_threshold, min_score) if min_score > 0 else base_threshold

    # ADX range filter — reject signals when market is choppy (ADX low).
    # Trending markets (ADX > 25) are where breakout strategies win; below 20
    # is no-trend territory where every direction signal whipsaws.
    adx = last.get("adx")
    adx_ok = (adx_min <= 0) or (adx is not None and not _pd.isna(adx) and adx >= adx_min)

    if bull >= threshold and bull > bear:
        if not adx_ok:
            return LiveSignal("NO_TRADE", spot, 0, 0,
                              f"bull-setup blocked: ADX={adx:.1f}<{adx_min:.0f} (chop)",
                              bull)
        return LiveSignal(
            "BUY_CALL", spot,
            stop_spot=spot * (1 - stop_pct),
            target_spot=spot * (1 + target_pct),
            reason=" + ".join(bull_r) + f" + ADX{adx:.0f}", score=bull,
        )
    if bear >= threshold and bear > bull:
        if not adx_ok:
            return LiveSignal("NO_TRADE", spot, 0, 0,
                              f"bear-setup blocked: ADX={adx:.1f}<{adx_min:.0f} (chop)",
                              bear)
        return LiveSignal(
            "BUY_PUT", spot,
            stop_spot=spot * (1 + stop_pct),
            target_spot=spot * (1 - target_pct),
            reason=" + ".join(bear_r) + f" + ADX{adx:.0f}", score=bear,
        )
    # If a direction met the base threshold but fell short of --min-score, say so
    # explicitly (distinguishes "filtered by min-score" from a genuinely mixed tape).
    best = max(bull, bear)
    if min_score > 0 and best >= base_threshold and best < threshold:
        side = "bull" if bull > bear else "bear"
        return LiveSignal("NO_TRADE", spot, 0, 0,
                          f"{side}={best} below min-score {threshold}", best)
    return LiveSignal("NO_TRADE", spot, 0, 0,
                       f"mixed (bull={bull}, bear={bear})", best)


# ============================================================================
# Option contract resolver — finds the right instrument_token + lot_size
# ============================================================================
@dataclass
class OptionContract:
    tradingsymbol: str       # e.g. "NIFTY2452823650CE"
    instrument_token: int
    exchange: str            # "NFO" or "BFO"
    lot_size: int
    expiry: datetime
    strike: float
    option_type: str         # "CE" or "PE"


class OptionChainResolver:
    """Resolves option contracts via Kite's actual instruments dump.

    The dump contains all live contracts (each with real expiry date,
    strike, lot_size, tradingsymbol, instrument_token). We discover what
    actually exists rather than computing expiry-by-weekday math, which
    breaks every time SEBI/NSE/BSE changes the rules (NIFTY weekly moved
    Thu → Tue in 2025, BANKNIFTY weeklies removed, etc.).

    Caches the per-exchange dump (~5 MB) on first access.
    """

    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self._instruments_cache: Dict[str, list] = {}

    def _instruments(self, exchange: str) -> list:
        if exchange not in self._instruments_cache:
            log.info(f"Fetching {exchange} instruments dump from Kite...")
            dump = self.kite.instruments(exchange)
            self._instruments_cache[exchange] = dump
            log.info(f"  -> {len(dump)} instruments cached for {exchange}")
        return self._instruments_cache[exchange]

    def _options_for(self, underlying_name: str) -> list:
        """All CE/PE rows for the given underlying name, across all expiries."""
        exch = EXCHANGE_FOR.get(underlying_name)
        if not exch:
            return []
        return [
            i for i in self._instruments(exch)
            if i.get("name") == underlying_name
            and i.get("instrument_type") in ("CE", "PE")
        ]

    def list_expiries(self, underlying_name: str):
        """Sorted list of unique expiry dates (today or later) for this underlying."""
        from datetime import date as _date
        today = now_ist().date()
        opts = self._options_for(underlying_name)
        return sorted({i["expiry"] for i in opts if i.get("expiry") and i["expiry"] >= today})

    def nearest_expiry(self, underlying_name: str):
        """The soonest non-past expiry. Returns None if none found."""
        exps = self.list_expiries(underlying_name)
        return exps[0] if exps else None

    def available_strikes(self, underlying_name: str, expiry, option_type: str = "CE"):
        """Sorted strikes that actually exist for a given expiry + CE/PE."""
        opts = self._options_for(underlying_name)
        return sorted({
            float(i["strike"]) for i in opts
            if i.get("expiry") == expiry and i.get("instrument_type") == option_type
        })

    def find_contract(self, underlying_name: str, spot: float,
                       option_type: str,
                       max_premium_cost: float = 0.0) -> Optional[OptionContract]:
        """Auto-discover the nearest valid contract.

        Normal mode (max_premium_cost <= 0):
          1. Pick soonest expiry
          2. Pick strike closest to spot (ATM)
          3. Return full contract

        Budget-constrained mode (max_premium_cost > 0):
          Picks the closest-to-ATM strike whose (premium × lot_size) fits
          within the budget. Goes slightly OTM if ATM is too expensive.
          Used by --small-capital mode.
        """
        exch = EXCHANGE_FOR.get(underlying_name)
        if not exch:
            log.error(f"No exchange mapping for {underlying_name}")
            return None

        expiry = self.nearest_expiry(underlying_name)
        if expiry is None:
            log.warning(f"{underlying_name}: no future expiries in {exch} dump")
            return None

        strikes = self.available_strikes(underlying_name, expiry, option_type)
        if not strikes:
            log.warning(f"{underlying_name} {expiry}: no {option_type} strikes available")
            return None

        # Budget-aware path — try ATM first, then increasingly OTM until budget fits
        if max_premium_cost > 0:
            # For CE, OTM = strikes ABOVE spot (cheaper). For PE, OTM = BELOW spot.
            ordered = sorted(strikes, key=lambda s: abs(s - spot))  # nearest first
            opts = self._options_for(underlying_name)
            for candidate_strike in ordered[:20]:   # try up to 20 strikes near ATM
                match = next(
                    (i for i in opts
                     if i.get("expiry") == expiry
                     and i.get("instrument_type") == option_type
                     and float(i.get("strike", 0)) == candidate_strike),
                    None,
                )
                if not match:
                    continue
                lot = int(match.get("lot_size") or 0)
                if lot <= 0:
                    continue
                # Peek at the premium via Kite quote
                key = f"{exch}:{match['tradingsymbol']}"
                try:
                    q = self.kite.quote([key])
                    prem = float(q[key]["last_price"])
                except Exception:
                    continue
                cost = prem * lot
                if cost <= max_premium_cost and prem > 0:
                    from datetime import datetime as _dt
                    exp_dt = _dt.combine(expiry, _dt.min.time()).replace(
                        hour=15, minute=30, tzinfo=IST
                    )
                    contract = OptionContract(
                        tradingsymbol=match["tradingsymbol"],
                        instrument_token=int(match["instrument_token"]),
                        exchange=exch, lot_size=lot, expiry=exp_dt,
                        strike=candidate_strike, option_type=option_type,
                    )
                    log.info(
                        f"Budget-fit {underlying_name}: {contract.tradingsymbol} "
                        f"(strike={int(candidate_strike)}, premium=Rs {prem:.2f}, "
                        f"lot cost=Rs {cost:.0f}, budget=Rs {max_premium_cost:.0f})"
                    )
                    return contract
            log.warning(
                f"{underlying_name}: no {option_type} strike fits budget "
                f"Rs {max_premium_cost:.0f}"
            )
            return None

        # Normal ATM path
        atm = min(strikes, key=lambda s: abs(s - spot))
        opts = self._options_for(underlying_name)
        match = next(
            (i for i in opts
             if i.get("expiry") == expiry
             and i.get("instrument_type") == option_type
             and float(i.get("strike", 0)) == atm),
            None,
        )
        if not match:
            log.warning(
                f"Internal: strike {atm} listed but contract not found "
                f"for {underlying_name} {expiry} {option_type}"
            )
            return None

        from datetime import datetime as _dt
        exp_dt = _dt.combine(expiry, _dt.min.time()).replace(
            hour=15, minute=30, tzinfo=IST
        )
        contract = OptionContract(
            tradingsymbol=match["tradingsymbol"],
            instrument_token=int(match["instrument_token"]),
            exchange=exch,
            lot_size=int(match.get("lot_size") or 0),
            expiry=exp_dt,
            strike=atm,
            option_type=option_type,
        )
        log.info(
            f"Resolved {underlying_name}: {contract.tradingsymbol} "
            f"(strike={int(atm)}, expiry={expiry}, lot={contract.lot_size})"
        )
        return contract

    # Kept for backward compatibility — calls the new auto-resolver
    def resolve(self, underlying_name: str, strike: int, expiry: datetime,
                option_type: str) -> Optional[OptionContract]:
        """Deprecated. Use find_contract() instead — auto-discovers expiry+strike."""
        return self.find_contract(underlying_name, float(strike), option_type)

    def get_quote(self, contract: OptionContract) -> Optional[float]:
        """Returns last_price for the contract, or None on failure."""
        key = f"{contract.exchange}:{contract.tradingsymbol}"
        try:
            q = self.kite.quote([key])
            return float(q[key]["last_price"])
        except Exception as e:
            log.warning(f"Quote failed for {key}: {e}")
            return None

    def get_spot(self, underlying: Underlying) -> Optional[float]:
        """Live spot for the index via Kite quote on the underlying symbol."""
        # NIFTY 50, NIFTY BANK, SENSEX
        symbol_map = {
            "NIFTY": "NSE:NIFTY 50",
            "BANKNIFTY": "NSE:NIFTY BANK",
            "FINNIFTY": "NSE:NIFTY FIN SERVICE",
            "SENSEX": "BSE:SENSEX",
            "BANKEX": "BSE:BANKEX",
        }
        sym = symbol_map.get(underlying.name)
        if not sym:
            return None
        try:
            q = self.kite.quote([sym])
            return float(q[sym]["last_price"])
        except Exception as e:
            log.warning(f"Spot quote failed for {sym}: {e}")
            return None


# ============================================================================
# Position sizing
# ============================================================================
def compute_lots(premium: float, lot_size: int,
                 spot: float, stop_spot: float,
                 max_rupee_risk: float, capital: float) -> int:
    """Returns number of LOTS to buy.

    Spot move from entry to stop, times delta ≈ 0.5 (ATM), gives premium loss
    per unit. Risk per lot = premium_loss_per_unit × lot_size.
    """
    if premium <= 0 or lot_size <= 0:
        return 0
    spot_dist = abs(spot - stop_spot)
    if spot_dist <= 0:
        return 0
    # Approx delta of ATM = 0.5 — adjust if you trade ITM/OTM
    premium_loss_per_unit = spot_dist * 0.5
    risk_per_lot = premium_loss_per_unit * lot_size
    if risk_per_lot <= 0:
        return 0
    by_risk = math.floor(max_rupee_risk / risk_per_lot)
    # Cap by capital — never spend more than 30% of capital on a single trade
    max_premium_spend = capital * 0.30
    cost_per_lot = premium * lot_size
    by_capital = math.floor(max_premium_spend / cost_per_lot) if cost_per_lot > 0 else 0
    return max(0, min(by_risk, by_capital))


# ============================================================================
# Position tracking
# ============================================================================
@dataclass
class Position:
    underlying: str
    contract: OptionContract
    direction: str            # "BUY_CALL" / "BUY_PUT"
    qty_lots: int
    qty_units: int            # lots × lot_size
    entry_premium: float
    entry_spot: float
    stop_spot: float
    target_spot: float
    entry_time: datetime
    order_id: str = ""
    high_water_spot: float = 0.0
    # Trailing quick-profit (ratchet lock) state. Once unrealized profit first
    # reaches the target, `trail_armed` flips True, `peak_profit_rs` tracks the
    # high-water mark, and `lock_floor_rs` is the ratcheted floor (only rises).
    trail_armed: bool = False
    peak_profit_rs: float = 0.0
    lock_floor_rs: float = 0.0
    # Minimum seconds before stop/target checks fire — lets the entry fill settle
    # and prevents instant-exit-on-noise (a real bug we hit on 2026-05-25).
    min_hold_seconds: int = 60


# ============================================================================
# Order placement
# ============================================================================
class OptionExecutor:
    def __init__(self, kite: KiteConnect):
        self.kite = kite

    def _place(self, contract: OptionContract, side: str, qty_units: int,
               tag: str = "") -> Optional[str]:
        if qty_units <= 0:
            log.warning(f"Refusing to place order with qty={qty_units}")
            return None
        if OPTIONS_EXECUTION.dry_run:
            fake_id = f"DRY-{int(time.time())}"
            log.info(
                f"[DRY-RUN] {side} {contract.tradingsymbol} qty={qty_units} "
                f"(order_id={fake_id})"
            )
            return fake_id
        for attempt in range(1, OPTIONS_EXECUTION.max_retries + 1):
            try:
                order_kwargs = dict(
                    variety=OPTIONS_EXECUTION.variety,
                    exchange=contract.exchange,
                    tradingsymbol=contract.tradingsymbol,
                    transaction_type=side,
                    quantity=int(qty_units),
                    product=OPTIONS_EXECUTION.product,
                    order_type=OPTIONS_EXECUTION.order_type,
                    tag=tag[:20] if tag else None,
                )
                # Kite requires market_protection on MARKET F&O orders.
                if OPTIONS_EXECUTION.order_type.upper() == "MARKET":
                    order_kwargs["market_protection"] = OPTIONS_EXECUTION.market_protection_pct
                order_id = self.kite.place_order(**order_kwargs)
                log.info(f"Order placed: id={order_id} {side} "
                         f"{contract.tradingsymbol} qty={qty_units}")
                return str(order_id)
            except Exception as e:
                log.warning(f"Order attempt {attempt} failed: {e}")
                if attempt < OPTIONS_EXECUTION.max_retries:
                    time.sleep(OPTIONS_EXECUTION.retry_backoff_sec * attempt)
        return None

    def buy(self, contract: OptionContract, qty_units: int, tag: str = "") -> Optional[str]:
        return self._place(contract, "BUY", qty_units, tag)

    def sell(self, contract: OptionContract, qty_units: int, tag: str = "") -> Optional[str]:
        return self._place(contract, "SELL", qty_units, tag)

    def kite_position_qty(self, tradingsymbol: str) -> int:
        """Live qty for a contract per Kite (positive=long, negative=short, 0=flat).
        Used to detect bot/broker desync — if bot thinks no position but Kite has
        one (or vice versa), we abort the entry to prevent duplicates."""
        if self.kite is None:
            return 0
        try:
            nets = self.kite.positions().get("net", [])
        except Exception as e:
            log.warning(f"Could not fetch positions for reconcile: {e}")
            return 0
        for p in nets:
            if p.get("tradingsymbol") == tradingsymbol:
                return int(p.get("quantity") or 0)
        return 0


# ============================================================================
# Risk + bookkeeping
# ============================================================================
class OptionsRiskManager:
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.realized_pnl: float = 0.0
        self.trades_today: int = 0
        self.halted: bool = False
        self.halt_reason: str = ""
        self.session_date = datetime.now(IST).date()
        # Tracks when we last exited each underlying — used for cooldown.
        # Cleared on session reset. `last_exit_was_loss` lets can_open() apply
        # a longer cooldown after losing trades than after winners.
        self.last_exit_time: Dict[str, datetime] = {}
        self.last_exit_was_loss: Dict[str, bool] = {}

    def reset_if_new_session(self):
        today = datetime.now(IST).date()
        if today != self.session_date:
            log.info(f"New trading session {today} — resetting options daily state")
            self.session_date = today
            self.realized_pnl = 0
            self.trades_today = 0
            self.halted = False
            self.halt_reason = ""
            self.last_exit_time = {}
            self.last_exit_was_loss = {}

    def can_open(self, underlying_name: str) -> tuple[bool, str]:
        self.reset_if_new_session()
        if self.halted:
            return False, f"halted: {self.halt_reason}"
        if underlying_name in self.positions:
            return False, "already in position"
        if len(self.positions) >= OPTIONS_RISK.max_open_positions:
            return False, f"max positions ({OPTIONS_RISK.max_open_positions})"
        if (OPTIONS_RISK.max_trades_per_day > 0
                and self.trades_today >= OPTIONS_RISK.max_trades_per_day):
            return False, (f"max trades/day reached "
                           f"({self.trades_today}/{OPTIONS_RISK.max_trades_per_day}) "
                           f"-- prevents revenge trading")
        # Cooldown — no re-entry on same underlying for N seconds after exit.
        # Losses get a longer lockout (cooldown_sec_after_loss) than wins to
        # break the revenge-trade pattern that bleeds out a session in chop.
        last_exit = self.last_exit_time.get(underlying_name)
        if last_exit is not None:
            was_loss = self.last_exit_was_loss.get(underlying_name, False)
            cooldown = (OPTIONS_RISK.cooldown_sec_after_loss if was_loss
                        else OPTIONS_RISK.cooldown_sec_after_exit)
            if cooldown > 0:
                elapsed = (datetime.now(IST) - last_exit).total_seconds()
                if elapsed < cooldown:
                    remaining = cooldown - elapsed
                    kind = "post-loss" if was_loss else "post-exit"
                    return False, (f"{kind} cooldown active "
                                   f"({remaining:.0f}s remaining of {cooldown}s)")
        return True, ""

    def record_open(self, pos: Position):
        self.positions[pos.underlying] = pos
        self.trades_today += 1

    def record_close(self, name: str, exit_premium: float) -> float:
        pos = self.positions.pop(name, None)
        if not pos:
            return 0.0
        pnl = (exit_premium - pos.entry_premium) * pos.qty_units
        self.realized_pnl += pnl
        # Start cooldown clock for this underlying. Stash whether this exit was
        # a loss so can_open() can apply the longer post-loss cooldown next time.
        self.last_exit_time[name] = datetime.now(IST)
        self.last_exit_was_loss[name] = (pnl < 0)
        self._check_daily_loss_limit()
        return pnl

    def _check_daily_loss_limit(self):
        if self.halted:
            return

        # Loss limit: absolute Rs takes priority over pct if both set
        if OPTIONS_RISK.daily_loss_limit_rs > 0:
            loss_limit = -abs(OPTIONS_RISK.daily_loss_limit_rs)
        else:
            loss_limit = -abs(OPTIONS_RISK.capital * OPTIONS_RISK.daily_loss_limit_pct)

        if self.realized_pnl <= loss_limit:
            self.halted = True
            self.halt_reason = (
                f"daily loss Rs {self.realized_pnl:+.0f} <= limit Rs {loss_limit:.0f}"
            )
            log.error(f"OPTIONS DAILY LOSS LIMIT HIT — halting. {self.halt_reason}")
            send_telegram(
                f"🚨 <b>OPTIONS DAILY LOSS LIMIT HIT</b>\n"
                f"Realized P&amp;L: ₹{self.realized_pnl:.0f}\n"
                f"Limit: ₹{loss_limit:.0f}\n"
                f"New entries halted for the day."
            )
            return

        # Profit target: lock in gains once threshold reached
        if (OPTIONS_RISK.daily_profit_target_rs > 0
                and self.realized_pnl >= OPTIONS_RISK.daily_profit_target_rs):
            self.halted = True
            self.halt_reason = (
                f"daily profit Rs {self.realized_pnl:+.0f} >= target "
                f"Rs {OPTIONS_RISK.daily_profit_target_rs:.0f}"
            )
            log.info(f"OPTIONS DAILY PROFIT TARGET HIT — locking in. {self.halt_reason}")
            send_telegram(
                f"🎯 <b>DAILY PROFIT TARGET HIT — bot halting</b>\n"
                f"Realized P&amp;L: ₹{self.realized_pnl:+.0f}\n"
                f"Target: ₹{OPTIONS_RISK.daily_profit_target_rs:.0f}\n"
                f"No more entries today. Locked in. ✅"
            )


# ============================================================================
# Time helpers
# ============================================================================
def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    n = now_ist().time()
    return dtime(*config.TIMING.market_open_hhmm) <= n <= dtime(*config.TIMING.market_close_hhmm)


def is_within_opening_skip() -> bool:
    """True during the post-open observation window — no NEW entries yet.
    e.g. skip_open_minutes=60 with a 09:15 open blocks entries until 10:15.
    Exits/stops are unaffected; this only gates new entries."""
    if OPTIONS_RISK.skip_open_minutes <= 0:
        return False
    oh, om = config.TIMING.market_open_hhmm
    n = now_ist().time()
    return (n.hour * 60 + n.minute) < (oh * 60 + om) + OPTIONS_RISK.skip_open_minutes


def is_past_last_entry() -> bool:
    """True once we're past the last-entry cutoff — block NEW entries near close
    (theta bleed / no time to work before square-off). Exits still run."""
    if OPTIONS_RISK.last_entry_hhmm is None:
        return False
    return now_ist().time() >= dtime(*OPTIONS_RISK.last_entry_hhmm)


def is_squareoff_time() -> bool:
    return now_ist().time() >= dtime(*OPTIONS_RISK.daily_squareoff_hhmm)


def is_expiry_day_exit_time(pos: Position) -> bool:
    """True if today IS the position's expiry day AND we've reached the
    expiry-day exit cutoff (e.g. 14:30)."""
    today = now_ist().date()
    if pos.contract.expiry.date() != today:
        return False
    return now_ist().time() >= dtime(*OPTIONS_RISK.expiry_day_exit_hhmm)


# ============================================================================
# CSV logging
# ============================================================================
OPT_FIELDS = [
    "timestamp_ist", "event", "underlying", "contract", "side",
    "qty_lots", "qty_units", "premium", "spot", "stop_spot", "target_spot",
    "entry_premium", "pnl", "order_id", "reason",
]


def log_order(event: str, **kw):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    write_header = not os.path.exists(OPTIONS_LOG_CSV)
    row = {"timestamp_ist": now_ist().isoformat(timespec="seconds"), "event": event}
    row.update(kw)
    with open(OPTIONS_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OPT_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in OPT_FIELDS})


# ============================================================================
# Main bot
# ============================================================================
class OptionsBot:
    def __init__(self):
        log.info("=" * 60)
        log.info("Options trading bot initializing")
        log.info(f"  Capital: Rs {OPTIONS_RISK.capital:,.0f}  |  "
                 f"Risk/trade: {OPTIONS_RISK.risk_per_trade_pct*100:.1f}%  "
                 f"(max Rs {OPTIONS_RISK.capital * OPTIONS_RISK.risk_per_trade_pct:.0f})")
        if OPTIONS_RISK.daily_loss_limit_rs > 0:
            log.info(f"  Daily loss limit: -Rs {OPTIONS_RISK.daily_loss_limit_rs:.0f}  "
                     f"(absolute Rs, overrides pct)")
        else:
            log.info(f"  Daily loss cap: {OPTIONS_RISK.daily_loss_limit_pct*100:.0f}%  "
                     f"(Rs {OPTIONS_RISK.capital * OPTIONS_RISK.daily_loss_limit_pct:.0f})")
        if OPTIONS_RISK.daily_profit_target_rs > 0:
            log.info(f"  Daily profit target: +Rs {OPTIONS_RISK.daily_profit_target_rs:.0f}  "
                     f"(halts entries when hit)")
        else:
            log.info(f"  Daily profit target: DISABLED (no auto-halt on gain)")
        log.info(f"  Expiry-day exit: {OPTIONS_RISK.expiry_day_exit_hhmm[0]:02d}:"
                 f"{OPTIONS_RISK.expiry_day_exit_hhmm[1]:02d} IST")
        if OPTIONS_RISK.quick_profit_target_rs > 0:
            if OPTIONS_RISK.quick_profit_trail_rs > 0:
                log.info(f"  Quick-profit exit: arm at +Rs {OPTIONS_RISK.quick_profit_target_rs:.0f}, "
                         f"then RATCHET-lock in Rs {OPTIONS_RISK.quick_profit_trail_rs:.0f} steps "
                         f"(floor only rises; max giveback ~1 step)")
            else:
                log.info(f"  Quick-profit exit: +Rs {OPTIONS_RISK.quick_profit_target_rs:.0f} per trade (immediate)")
        else:
            log.info(f"  Quick-profit exit: DISABLED")
        if OPTIONS_RISK.quick_loss_limit_rs > 0:
            log.info(f"  Quick-loss exit:   -Rs {OPTIONS_RISK.quick_loss_limit_rs:.0f} per trade")
        else:
            log.info(f"  Quick-loss exit:   DISABLED")
        cd = OPTIONS_RISK.cooldown_sec_after_exit
        cdl = OPTIONS_RISK.cooldown_sec_after_loss
        log.info(f"  Cooldown after win:  {cd}s ({cd//60} min) per underlying")
        log.info(f"  Cooldown after loss: {cdl}s ({cdl//60} min) per underlying")
        if OPTIONS_RISK.adx_min_threshold > 0:
            log.info(f"  Range filter (ADX):  block entries when ADX < "
                     f"{OPTIONS_RISK.adx_min_threshold:.0f}")
        else:
            log.info(f"  Range filter (ADX):  DISABLED")
        if OPTIONS_RISK.min_score > 0:
            log.info(f"  Min-score filter:    require >= {OPTIONS_RISK.min_score} confirmations "
                     f"(skips marginal setups)")
        else:
            log.info(f"  Min-score filter:    DISABLED (default 3/4 threshold)")
        if OPTIONS_RISK.skip_open_minutes > 0:
            oh, om = config.TIMING.market_open_hhmm
            cm = oh * 60 + om + OPTIONS_RISK.skip_open_minutes
            log.info(f"  Opening filter:      observe first {OPTIONS_RISK.skip_open_minutes} min — "
                     f"no entries before {cm//60:02d}:{cm%60:02d}")
        else:
            log.info(f"  Opening filter:      DISABLED (trades from open)")
        if OPTIONS_RISK.last_entry_hhmm is not None:
            le = OPTIONS_RISK.last_entry_hhmm
            log.info(f"  Last entry:          {le[0]:02d}:{le[1]:02d} — no new buys into the close")
        else:
            log.info(f"  Last entry:          DISABLED (entries until square-off)")
        if OPTIONS_RISK.max_premium_above_open_pct > 0:
            log.info(f"  Premium-chase guard: block entries when premium > "
                     f"{OPTIONS_RISK.max_premium_above_open_pct*100:.0f}% above today's open")
        else:
            log.info(f"  Premium-chase guard: DISABLED")
        log.info(f"  RSI confluence:      bull {OPTIONS_RISK.rsi_bull_min:.0f}-"
                 f"{OPTIONS_RISK.rsi_bull_max:.0f}, "
                 f"bear {OPTIONS_RISK.rsi_bear_min:.0f}-"
                 f"{OPTIONS_RISK.rsi_bear_max:.0f}")
        if OPTIONS_RISK.small_capital_mode:
            log.info(f"  Active underlyings: {OPTIONS_RISK.small_underlyings}")
        log.info(f"  DRY-RUN: {OPTIONS_EXECUTION.dry_run}")
        self.kite = make_kite()
        self.resolver = OptionChainResolver(self.kite)
        self.executor = OptionExecutor(self.kite)
        self.risk = OptionsRiskManager()
        # Real-time index data fetcher (replaces yfinance's 15-min-delayed feed)
        self.index_data = IndexDataFetcher(self.kite)
        self._running = True
        self._last_signal_eval = 0.0
        # On startup, reconcile against Kite's open positions. If any open
        # options position exists for our underlyings, warn loudly — the user
        # should manually close it OR the bot can adopt it.
        self._reconcile_open_positions_at_startup()

    def _reconcile_open_positions_at_startup(self):
        """Check Kite for already-open positions on our watch underlyings.
        Loudly warn so we don't open a duplicate by mistake."""
        if OPTIONS_EXECUTION.dry_run:
            return
        try:
            nets = self.kite.positions().get("net", [])
        except Exception as e:
            log.warning(f"Startup reconcile failed: {e}")
            return
        # Filter to open options positions matching our underlyings
        our_names = {u.name for u in UNDERLYINGS}
        open_pos = [p for p in nets
                    if p.get("quantity", 0) != 0
                    and any(name in (p.get("tradingsymbol") or "") for name in our_names)]
        if not open_pos:
            log.info("Startup reconcile: no pre-existing options positions found")
            return
        log.warning("=" * 60)
        log.warning("STARTUP RECONCILE — found OPEN positions in your Kite account:")
        for p in open_pos:
            log.warning(
                f"  {p['tradingsymbol']}  qty={p['quantity']}  "
                f"avg=Rs {p['average_price']:.2f}  m2m=Rs {p.get('m2m', 0):+.0f}"
            )
        log.warning("Bot will NOT auto-open a duplicate on these underlyings.")
        log.warning("Either close them manually in Kite, or wait — the duplicate")
        log.warning("guard will block bot entries on the same contract.")
        log.warning("=" * 60)
        send_telegram(
            "⚠️ <b>Startup reconcile</b>\n"
            f"Found {len(open_pos)} open position(s) in your account:\n"
            + "\n".join(f"• {p['tradingsymbol']} qty={p['quantity']}" for p in open_pos)
            + "\nBot will not open duplicates."
        )

    # ------------------------------------------------------------------ #
    # Entry / exit
    # ------------------------------------------------------------------ #
    def try_enter(self, sig: OptionsSignal) -> None:
        # Opening observation window — no new entries until the market has been
        # open for skip_open_minutes (avoids the whipsaw/wide-spread first hour).
        if is_within_opening_skip():
            oh, om = config.TIMING.market_open_hhmm
            cutoff_min = oh * 60 + om + OPTIONS_RISK.skip_open_minutes
            log.info(
                f"Entry blocked for {sig.underlying.name}: observing market "
                f"(first {OPTIONS_RISK.skip_open_minutes} min after open; "
                f"entries start {cutoff_min // 60:02d}:{cutoff_min % 60:02d})"
            )
            return
        # No new entries near close — theta bleed / no time before square-off.
        if is_past_last_entry():
            le = OPTIONS_RISK.last_entry_hhmm
            log.info(
                f"Entry blocked for {sig.underlying.name}: past last-entry cutoff "
                f"{le[0]:02d}:{le[1]:02d} (no new buys into the close)"
            )
            return
        allowed, reason = self.risk.can_open(sig.underlying.name)
        if not allowed:
            log.info(f"Entry blocked for {sig.underlying.name}: {reason}")
            return

        opt_type = "CE" if sig.direction == "BUY_CALL" else "PE"
        # Auto-discover the right expiry + ATM strike from the live Kite dump.
        # In small-capital mode, find the cheapest near-ATM strike that fits budget.
        budget = OPTIONS_RISK.max_premium_cost_per_trade if OPTIONS_RISK.small_capital_mode else 0.0
        contract = self.resolver.find_contract(
            sig.underlying.name, spot=sig.spot, option_type=opt_type,
            max_premium_cost=budget,
        )
        if not contract:
            return

        # RECONCILE: ensure Kite agrees we don't already have this position.
        # Catches bot↔broker desync (bot crashed, restarted, lost memory etc).
        # Skipped in dry-run since no real position would exist.
        if not OPTIONS_EXECUTION.dry_run:
            kite_qty = self.executor.kite_position_qty(contract.tradingsymbol)
            if kite_qty != 0:
                log.warning(
                    f"Pre-entry reconcile: Kite shows existing qty={kite_qty} on "
                    f"{contract.tradingsymbol}. Skipping entry to prevent duplicate."
                )
                send_telegram(
                    f"⚠️ <b>Entry skipped — duplicate guard</b>\n"
                    f"{contract.tradingsymbol}\n"
                    f"Kite already has qty {kite_qty}. Bot will not double up."
                )
                return

        # Skip if it's already the expiry day past the cutoff
        if contract.expiry.date() == now_ist().date() and \
                now_ist().time() >= dtime(*OPTIONS_RISK.expiry_day_exit_hhmm):
            log.info(f"Skipping entry on {sig.underlying.name} — past expiry-day cutoff")
            return

        # --- Full quote for sanity checks (bid/ask/open) ---
        quote_key = f"{contract.exchange}:{contract.tradingsymbol}"
        try:
            full_q = self.kite.quote([quote_key])[quote_key]
        except Exception as e:
            log.warning(f"Could not get full quote for {contract.tradingsymbol}: {e}")
            return
        premium = float(full_q.get("last_price") or 0)
        if not premium:
            log.warning(f"Premium=0 for {contract.tradingsymbol} — skipping")
            return

        # SAFETY 1: Bid-ask spread sanity (skip illiquid strikes)
        depth = full_q.get("depth") or {}
        buy_d = depth.get("buy") or []
        sell_d = depth.get("sell") or []
        if buy_d and sell_d:
            bid = float(buy_d[0].get("price") or 0)
            ask = float(sell_d[0].get("price") or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid if mid > 0 else 1
                if spread_pct > OPTIONS_RISK.max_bid_ask_spread_pct:
                    log.info(
                        f"Entry blocked: bid-ask spread {spread_pct*100:.1f}% "
                        f"on {contract.tradingsymbol} exceeds limit "
                        f"{OPTIONS_RISK.max_bid_ask_spread_pct*100:.1f}% "
                        f"(bid Rs {bid:.2f} / ask Rs {ask:.2f})"
                    )
                    return

        # SAFETY 2: Minimum stop-distance (catches the "stop already breached" bug)
        spot_now_diff = abs(sig.spot - sig.stop_spot)
        if sig.spot > 0:
            stop_dist_pct = spot_now_diff / sig.spot
            if stop_dist_pct < OPTIONS_RISK.min_stop_distance_pct:
                log.warning(
                    f"Entry blocked: stop distance {stop_dist_pct*100:.2f}% on "
                    f"{contract.tradingsymbol} is below minimum "
                    f"{OPTIONS_RISK.min_stop_distance_pct*100:.2f}% — likely stale spot"
                )
                return

        # SAFETY 3: Premium hasn't spiked above today's open (avoid buying the high)
        # Guard is disabled when the limit is set to 0 (--max-premium-above-open 0).
        ohlc = full_q.get("ohlc") or {}
        day_open = float(ohlc.get("open") or 0)
        if day_open > 0 and OPTIONS_RISK.max_premium_above_open_pct > 0:
            above_open = (premium - day_open) / day_open
            if above_open > OPTIONS_RISK.max_premium_above_open_pct:
                log.info(
                    f"Entry blocked: premium Rs {premium:.2f} is {above_open*100:.0f}% "
                    f"above today's open Rs {day_open:.2f} on "
                    f"{contract.tradingsymbol}. Limit "
                    f"{OPTIONS_RISK.max_premium_above_open_pct*100:.0f}%."
                )
                return

        max_risk = OPTIONS_RISK.capital * OPTIONS_RISK.risk_per_trade_pct

        # Small-capital mode: skip risk-math sizing, always buy 1 lot
        # (the budget filter on find_contract already ensured affordability).
        if OPTIONS_RISK.force_one_lot:
            lot_cost = premium * contract.lot_size
            if lot_cost > OPTIONS_RISK.capital:
                log.info(
                    f"1 lot of {contract.tradingsymbol} costs Rs {lot_cost:.0f} > "
                    f"capital Rs {OPTIONS_RISK.capital:.0f}. Skipping."
                )
                return
            # Auto-size from capital + live lot price: buy as many lots as the
            # per-trade budget (95% of capital) affords. --max-lots, if set, is
            # an optional safety ceiling on top of the auto figure.
            budget = OPTIONS_RISK.max_premium_cost_per_trade or OPTIONS_RISK.capital
            lots = max(1, int(budget // lot_cost))
            if OPTIONS_RISK.max_lots > 0:
                lots = min(lots, OPTIONS_RISK.max_lots)
            if lots > 1:
                log.info(
                    f"Auto-sized {lots} lots of {contract.tradingsymbol} "
                    f"(lot cost Rs {lot_cost:.0f} x {lots} = Rs {lot_cost*lots:.0f}, "
                    f"budget Rs {budget:.0f}"
                    + (f", cap {OPTIONS_RISK.max_lots}" if OPTIONS_RISK.max_lots > 0 else "")
                    + ")"
                )
        else:
            lots = compute_lots(
                premium=premium, lot_size=contract.lot_size,
                spot=sig.spot, stop_spot=sig.stop_spot,
                max_rupee_risk=max_risk, capital=OPTIONS_RISK.capital,
            )
            if lots <= 0:
                log.info(
                    f"Sizing returned 0 lots for {contract.tradingsymbol} "
                    f"(premium Rs {premium:.2f}, lot Rs {premium * contract.lot_size:.0f}). "
                    f"Skipping -- risk budget Rs {max_risk:.0f} too tight."
                )
                return
            # Apply the --max-lots cap (if set) on top of risk-based sizing.
            if OPTIONS_RISK.max_lots > 0 and lots > OPTIONS_RISK.max_lots:
                log.info(
                    f"Capping {lots} -> {OPTIONS_RISK.max_lots} lots (--max-lots)"
                )
                lots = OPTIONS_RISK.max_lots

        qty_units = lots * contract.lot_size
        order_id = self.executor.buy(contract, qty_units,
                                      tag=f"OPT_{sig.underlying.name}")
        if not order_id:
            log.error(f"BUY order failed for {contract.tradingsymbol}")
            return

        pos = Position(
            underlying=sig.underlying.name,
            contract=contract,
            direction=sig.direction,
            qty_lots=lots, qty_units=qty_units,
            entry_premium=premium,
            entry_spot=sig.spot,
            stop_spot=sig.stop_spot,
            target_spot=sig.target_spot,
            entry_time=now_ist(),
            order_id=order_id,
            high_water_spot=sig.spot,
        )
        self.risk.record_open(pos)
        log_order(
            "ENTRY", underlying=sig.underlying.name,
            contract=contract.tradingsymbol, side="BUY",
            qty_lots=lots, qty_units=qty_units, premium=premium,
            spot=sig.spot, stop_spot=sig.stop_spot, target_spot=sig.target_spot,
            entry_premium=premium, order_id=order_id, reason=sig.reason,
        )

        side = "CALL" if sig.direction == "BUY_CALL" else "PUT"
        emoji = "🟢" if side == "CALL" else "🔴"
        send_telegram(
            f"{emoji} <b>BUY {side} — {sig.underlying.name}</b>\n"
            f"<b>Contract:</b> {contract.tradingsymbol}\n"
            f"<b>Qty:</b> {lots} lots ({qty_units} units)\n"
            f"<b>Entry premium:</b> ₹{premium:.2f}\n"
            f"<b>Spot:</b> {sig.spot:,.2f}\n"
            f"<b>Stop spot:</b> {sig.stop_spot:,.2f}\n"
            f"<b>Target spot:</b> {sig.target_spot:,.2f}\n"
            f"<b>Capital deployed:</b> ₹{premium * qty_units:,.0f}\n"
            f"<b>Max risk:</b> ≈ ₹{max_risk:.0f}\n"
            f"<b>Reason:</b> {sig.reason}\n"
            f"{'<i>[DRY-RUN]</i>' if OPTIONS_EXECUTION.dry_run else ''}"
        )

    def try_exit(self, name: str, reason: str) -> None:
        pos = self.risk.positions.get(name)
        if not pos:
            return
        exit_premium = self.resolver.get_quote(pos.contract) or pos.entry_premium
        order_id = self.executor.sell(pos.contract, pos.qty_units,
                                      tag=f"EXIT_{name}")
        if not order_id:
            log.error(f"EXIT order failed for {pos.contract.tradingsymbol}")
            return
        pnl = self.risk.record_close(name, exit_premium)
        log_order(
            "EXIT", underlying=name, contract=pos.contract.tradingsymbol,
            side="SELL", qty_lots=pos.qty_lots, qty_units=pos.qty_units,
            premium=exit_premium, entry_premium=pos.entry_premium,
            pnl=round(pnl, 2), order_id=order_id, reason=reason,
        )
        sign = "🟢" if pnl >= 0 else "🔴"
        send_telegram(
            f"{sign} <b>EXIT — {name}</b>\n"
            f"<b>Contract:</b> {pos.contract.tradingsymbol}\n"
            f"<b>Entry:</b> ₹{pos.entry_premium:.2f}   "
            f"<b>Exit:</b> ₹{exit_premium:.2f}\n"
            f"<b>P&amp;L:</b> ₹{pnl:+,.0f} ({(pnl / (pos.entry_premium * pos.qty_units)) * 100:+.2f}%)\n"
            f"<b>Reason:</b> {reason}\n"
            f"{'<i>[DRY-RUN]</i>' if OPTIONS_EXECUTION.dry_run else ''}"
        )

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    def check_stops_and_targets(self):
        for name in list(self.risk.positions.keys()):
            pos = self.risk.positions[name]
            u = next((x for x in UNDERLYINGS if x.name == name), None)
            if not u:
                continue
            spot = self.resolver.get_spot(u)
            if spot is None:
                continue

            # 14:30 expiry-day exit (highest priority)
            if is_expiry_day_exit_time(pos):
                self.try_exit(name, "EXPIRY_DAY_CUTOFF")
                continue

            # Catastrophic premium stop — even in "no SL" mode, force-exit
            # if premium drops by the configured fraction (default 60%).
            # Protects against losing 100% on a single trade.
            cur_premium = self.resolver.get_quote(pos.contract)
            if cur_premium is not None and pos.entry_premium > 0:
                drop_pct = 1 - (cur_premium / pos.entry_premium)
                if drop_pct >= OPTIONS_RISK.catastrophic_premium_stop_pct:
                    self.try_exit(
                        name,
                        f"CATASTROPHIC_PREMIUM_STOP_-{drop_pct*100:.0f}pct"
                    )
                    continue

                # QUICK-PROFIT exit. Two modes:
                #   trail == 0 : exit immediately when target is hit (original).
                #   trail  > 0 : arm a trailing lock at the target, let profit
                #               ride, and sell when it pulls back `trail` Rs below
                #               its peak — but never lock in less than the target.
                if OPTIONS_RISK.quick_profit_target_rs > 0:
                    unrealized = (cur_premium - pos.entry_premium) * pos.qty_units
                    # Stops are PER-LOT and scale with the position's lot count, so
                    # the per-unit exit distance stays constant no matter how many
                    # lots the bot auto-sized (1 lot => identical to original).
                    target = OPTIONS_RISK.quick_profit_target_rs * pos.qty_lots
                    trail = OPTIONS_RISK.quick_profit_trail_rs * pos.qty_lots
                    if trail <= 0:
                        if unrealized >= target:
                            self.try_exit(name, f"QUICK_PROFIT_+Rs{int(unrealized)}")
                            continue
                    else:
                        # Arm the ratchet the first time profit reaches the target.
                        if not pos.trail_armed and unrealized >= target:
                            pos.trail_armed = True
                            pos.peak_profit_rs = unrealized
                            pos.lock_floor_rs = target
                            log.info(
                                f"[{name}] quick-profit ratchet ARMED at "
                                f"+Rs{int(unrealized)} (target Rs{int(target)}, "
                                f"step Rs{int(trail)})"
                            )
                        if pos.trail_armed:
                            # Track the high-water mark of unrealized profit.
                            if unrealized > pos.peak_profit_rs:
                                pos.peak_profit_rs = unrealized
                            # Ratchet the locked floor UP in `trail`-sized steps:
                            #   floor = target + floor((peak - target) / step) * step
                            # It only ever rises (max() guard), so a collapse can
                            # never give back more than ~one step or go negative.
                            steps = int((pos.peak_profit_rs - target) // trail)
                            new_floor = target + steps * trail
                            if new_floor > pos.lock_floor_rs:
                                pos.lock_floor_rs = new_floor
                                log.info(
                                    f"[{name}] profit ratchet UP: locked "
                                    f"+Rs{int(pos.lock_floor_rs)} (peak "
                                    f"+Rs{int(pos.peak_profit_rs)})"
                                )
                            if unrealized < pos.lock_floor_rs:
                                self.try_exit(
                                    name,
                                    f"QUICK_PROFIT_RATCHET_+Rs{int(unrealized)}"
                                    f"_peak{int(pos.peak_profit_rs)}"
                                    f"_lock{int(pos.lock_floor_rs)}"
                                )
                                continue

                # QUICK-LOSS exit: cut loss at fixed Rs amount.
                # Tighter than spot stop or catastrophic — gives small predictable losses.
                if OPTIONS_RISK.quick_loss_limit_rs > 0:
                    unrealized = (cur_premium - pos.entry_premium) * pos.qty_units
                    # Per-lot, scaled by lot count (see quick-profit note above).
                    quick_loss = OPTIONS_RISK.quick_loss_limit_rs * pos.qty_lots
                    if unrealized <= -abs(quick_loss):
                        self.try_exit(
                            name,
                            f"QUICK_LOSS_-Rs{int(abs(unrealized))}"
                        )
                        continue

            # Minimum hold time — skip spot stop/target during first N seconds
            # after entry to absorb quote noise and fill jitter.
            # (Catastrophic premium stop + expiry cutoff above still apply.)
            elapsed = (now_ist() - pos.entry_time).total_seconds()
            if elapsed < pos.min_hold_seconds:
                continue

            # CALL: stop if spot below, target if above
            if pos.direction == "BUY_CALL":
                if spot <= pos.stop_spot:
                    self.try_exit(name, "STOP_HIT")
                elif spot >= pos.target_spot:
                    self.try_exit(name, "TARGET_HIT")
            else:  # PUT
                if spot >= pos.stop_spot:
                    self.try_exit(name, "STOP_HIT")
                elif spot <= pos.target_spot:
                    self.try_exit(name, "TARGET_HIT")

    def evaluate_signals(self):
        # In small-capital mode, restrict to the configured cheap underlyings
        active = (
            [u for u in UNDERLYINGS if u.name in OPTIONS_RISK.small_underlyings]
            if OPTIONS_RISK.small_capital_mode
            else UNDERLYINGS
        )
        for u in active:
            try:
                # FIXED 2026-05-25: use Kite real-time historical_data
                # instead of yfinance (which is 15-min delayed).
                # The delay caused stop_spot to be computed from stale data,
                # leading to instant stop-outs at entry.
                df = self.index_data.fetch_candles(u.name, days=5, interval="5minute")
                if df is None or len(df) < 60:
                    log.info(f"[{u.name}] insufficient data ({0 if df is None else len(df)} bars)")
                    continue
                live_sig = score_realtime(
                    df,
                    rsi_bull_min=OPTIONS_RISK.rsi_bull_min,
                    rsi_bull_max=OPTIONS_RISK.rsi_bull_max,
                    rsi_bear_min=OPTIONS_RISK.rsi_bear_min,
                    rsi_bear_max=OPTIONS_RISK.rsi_bear_max,
                    adx_min=OPTIONS_RISK.adx_min_threshold,
                    min_score=OPTIONS_RISK.min_score,
                )
                # Always log the score — gives visibility into what's happening
                log.info(
                    f"[{u.name}] signal: {live_sig.direction}  "
                    f"score={live_sig.score}/5  spot={live_sig.spot:.2f}  "
                    f"reason: {live_sig.reason}"
                )
                if live_sig.direction == "NO_TRADE":
                    continue
                # Resolve expiry for the eventual contract pick (still uses signal
                # for the underlying object — strike comes from resolver.find_contract)
                sig = OptionsSignal(
                    underlying=u,
                    direction=live_sig.direction,
                    spot=live_sig.spot,
                    strike=0,            # find_contract picks the real ATM
                    expiry=None,          # find_contract picks the real expiry
                    stop_spot=live_sig.stop_spot,
                    target_spot=live_sig.target_spot,
                    reason=live_sig.reason,
                    score=live_sig.score,
                    indicators={},
                )
            except Exception as e:
                log.warning(f"Signal failed for {u.name}: {e}")
                continue
            if sig.direction in ("BUY_CALL", "BUY_PUT"):
                current = self.risk.positions.get(u.name)
                if current is None:
                    self.try_enter(sig)
                elif current.direction != sig.direction:
                    # Flip — exit then enter
                    self.try_exit(u.name, "SIGNAL_FLIPPED")
                    self.try_enter(sig)

    def squareoff_all(self, reason: str):
        for name in list(self.risk.positions.keys()):
            self.try_exit(name, reason)

    def run(self):
        def handler(s, f):
            self._running = False
            log.info(f"Signal {s} — shutting down")
        signal_mod.signal(signal_mod.SIGINT, handler)
        try:
            signal_mod.signal(signal_mod.SIGTERM, handler)
        except (AttributeError, ValueError, OSError):
            pass

        mode_line = (
            "🚨 <b>LIVE TRADING</b> — real orders will be placed"
            if not OPTIONS_EXECUTION.dry_run
            else "📡 <b>SIGNALS-ONLY</b> — alerts only, no real orders"
        )
        send_telegram(
            f"🤖 <b>Options bot started</b>\n"
            f"{mode_line}\n"
            f"Capital: ₹{OPTIONS_RISK.capital:,.0f}  |  "
            f"Risk/trade: ₹{OPTIONS_RISK.capital * OPTIONS_RISK.risk_per_trade_pct:.0f}\n"
            f"Watching: NIFTY, BANKNIFTY, SENSEX"
        )

        while self._running:
            try:
                # Kill-switch check — create STOP.txt in c:\Stocks to halt cleanly
                # without needing to find the terminal window.
                if os.path.exists("STOP.txt"):
                    log.warning("Kill-switch detected (STOP.txt exists). Halting cleanly.")
                    if self.risk.positions:
                        self.squareoff_all("KILL_SWITCH_FILE")
                    send_telegram("🛑 <b>Options bot stopped via STOP.txt kill-switch</b>")
                    break

                if is_squareoff_time() and self.risk.positions:
                    self.squareoff_all("DAILY_SQUAREOFF_315PM")

                if not is_market_open():
                    time.sleep(30)
                    continue

                now = time.time()
                if now - self._last_signal_eval >= POLL_SIGNAL_SEC:
                    self.evaluate_signals()
                    self._last_signal_eval = now

                if self.risk.positions:
                    self.check_stops_and_targets()

                # Poll fast while holding (responsive exits / ratchet), slow when flat.
                time.sleep(POLL_EXIT_SEC if self.risk.positions else POLL_TICK_SEC)
            except kc_exc.TokenException as e:
                log.error(f"Kite TokenException — bot stopping: {e}")
                send_telegram(f"🚨 Options bot stopping: Kite auth failed ({e})")
                break
            except Exception as e:
                log.exception(f"Loop error: {e}")
                time.sleep(POLL_TICK_SEC)

        # Shutdown
        if self.risk.positions:
            log.info("Squaring off remaining positions before exit")
            self.squareoff_all("SHUTDOWN")
        send_telegram("🛑 <b>Options bot stopped</b>")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Options trading bot for NIFTY / BANKNIFTY / SENSEX.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "MODES:\n"
            "  --auto-orders off  (DEFAULT)\n"
            "      Signals-only mode. Telegram BUY/EXIT alerts only.\n"
            "      No real orders. Safe to run with no funds.\n\n"
            "  --auto-orders on\n"
            "      Live trading. Real orders go to Kite, real money moves.\n"
            "      Position size = (capital × 1%) ÷ (spot stop distance × delta).\n"
            "      Capital defaults to your live Kite available cash; override\n"
            "      with --capital.\n"
        ),
    )
    p.add_argument(
        "--auto-orders",
        choices=["on", "off"],
        default="off",
        help="Place real orders ('on') or signals-only ('off'). Default: off.",
    )
    p.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Override trading capital in rupees. If omitted, auto-fetched "
             "from your Kite account balance.",
    )
    p.add_argument(
        "--small-capital",
        action="store_true",
        help="SMALL-CAPITAL MODE (for Rs 1k-5k accounts): SENSEX-only, "
             "1 lot per signal, picks cheapest near-ATM strike that fits "
             "your budget. Skips the 1%%-risk math. Catastrophic premium "
             "stop at -60%% protects against full wipeout. AGGRESSIVE — "
             "expect high variance; some trades may lose 60%% on a single "
             "adverse move.",
    )
    p.add_argument(
        "--daily-loss-cap",
        type=float,
        default=None,
        help="Override daily loss cap as a fraction of capital (e.g. 0.6 "
             "= halt at -60%% of capital). Default 0.03 (3%%); in small-"
             "capital mode default is 0.60 (60%%).",
    )
    p.add_argument(
        "--quick-profit",
        type=float,
        default=None,
        help="Exit any position when its profit reaches this Rs amount "
             "(takes priority over spot target). Default 300. Set 0 to disable. "
             "Backtest-validated optimal range: 300-750.",
    )
    p.add_argument(
        "--quick-profit-trail",
        type=float,
        default=None,
        help="Ratchet-lock step (Rs). Once profit reaches --quick-profit, the bot "
             "lets it ride and locks in escalating floors in steps of this size. "
             "The locked floor only rises, so a collapse can't erase a banked gain "
             "or go negative (e.g. step 50, peak +968 -> locks +950). Smaller = "
             "tighter lock; larger = more room to run. Default 50. Set 0 to exit "
             "immediately when the target is hit.",
    )
    p.add_argument(
        "--quick-loss",
        type=float,
        default=None,
        help="Exit any position when its LOSS reaches this Rs amount (per trade). "
             "Tight per-trade stop. Fires before spot stop or catastrophic stop. "
             "Default 150. Set 0 to disable. Example: --quick-loss 150 exits when "
             "the trade has lost Rs 150.",
    )
    p.add_argument(
        "--max-trades",
        type=int,
        default=None,
        help="Maximum trades per day. Set 0 to disable cap (let daily loss/profit "
             "limits enforce halt instead). Default in small-capital mode: 3. "
             "Recommend 10-15 if you have quick-loss + loss-limit set.",
    )
    p.add_argument(
        "--max-lots",
        type=int,
        default=None,
        help="OPTIONAL safety ceiling on lots per trade. By default (0) the bot "
             "auto-sizes from capital and the live lot price — it buys as many "
             "lots as the per-trade budget (95%% of capital) affords (e.g. capital "
             "10k, lot Rs 2k -> 4 lots). Set >0 to cap that auto figure. Stops "
             "(--quick-profit / --quick-loss) are PER-LOT and scale with the lot "
             "count automatically, so per-unit risk stays constant.",
    )
    p.add_argument(
        "--cooldown",
        type=int,
        default=None,
        help="Seconds to wait before re-entering same underlying after a WIN. "
             "Default 1800 (30 min). Lower = more trades but more whipsaw risk. "
             "Try 600 (10 min) for higher trade frequency. Set 0 to disable.",
    )
    p.add_argument(
        "--cooldown-loss",
        type=int,
        default=None,
        help="Seconds to wait before re-entering same underlying after a LOSS. "
             "Defaults to 1800 (30 min). Set higher than --cooldown to "
             "discourage revenge trades. Effective cooldown after a loss = "
             "max(--cooldown, --cooldown-loss). Set 0 to disable.",
    )
    p.add_argument(
        "--adx-min",
        type=float,
        default=None,
        help="Minimum ADX (trend strength) required for any entry. ADX < 20 "
             "is choppy/range-bound — breakout signals whipsaw there. "
             "Default 20.0. Set 0 to disable the range filter.",
    )
    p.add_argument(
        "--skip-open-min",
        type=int,
        default=None,
        help="Observe the market for N minutes after the 09:15 open before "
             "taking ANY new entry (e.g. 60 = no entries until 10:15). Avoids "
             "the whipsaw/wide-spread first hour. Exits still work. Default 0.",
    )
    p.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Minimum confirmation score (0-4 for indices) required to enter. "
             "Default 0 = standard threshold (3/4 for indices). Set 4 to demand "
             "ALL confirmations incl. MACD — skips marginal 3/5 setups that "
             "whipsaw when a move is exhausting. Fewer trades, higher quality.",
    )
    p.add_argument(
        "--last-entry",
        type=str,
        default=None,
        metavar="HH:MM",
        help="Last IST time a NEW entry may open. No new buys after this (the "
             "closing window bleeds theta with no time to work before square-off). "
             "Default 14:45. Exits still run after it. Use 'off' to disable.",
    )
    p.add_argument(
        "--expiry-exit",
        type=str,
        default=None,
        metavar="HH:MM",
        help="On expiry day, stop new entries and force-exit open positions at "
             "this IST time. Default 15:15 (same as normal square-off). Set "
             "earlier (e.g. 14:30) to exit before the volatile expiry-close; "
             "the broker auto-squares MIS at ~15:15, so don't go past that.",
    )
    p.add_argument(
        "--rsi-bull-min",
        type=float,
        default=None,
        help="RSI must exceed this value for a bullish confirmation. "
             "Default 60 (tighter than original 50). Lower = more signals, "
             "more whipsaws near neutral.",
    )
    p.add_argument(
        "--rsi-bear-max",
        type=float,
        default=None,
        help="RSI must be below this value for a bearish confirmation. "
             "Default 40 (tighter than original 50). Higher = more signals, "
             "more whipsaws near neutral.",
    )
    p.add_argument(
        "--all-indices",
        action="store_true",
        help="In small-capital mode, trade ALL indices — SENSEX + BANKNIFTY + "
             "NIFTY + FINNIFTY (default is SENSEX only). More signal sources. "
             "Larger-lot indices (NIFTY 75, FINNIFTY 65, both monthly) fit the "
             "budget only when premium is low enough, else skipped with 'no "
             "strike fits budget'.",
    )
    p.add_argument(
        "--max-premium-above-open",
        type=float,
        default=None,
        help="Anti-chase guard: block entry if the option premium has spiked "
             "more than this PERCENT above today's open (e.g. 30 = block at "
             ">30%% above open). Avoids buying the day's high / an IV spike. "
             "Default 30. Set 0 to disable the guard entirely.",
    )
    p.add_argument(
        "--profit-target",
        type=float,
        default=None,
        help="Halt new entries once day's realized P&L reaches this Rs amount. "
             "Locks in good days. Example: --profit-target 1500 stops the bot "
             "after you've made Rs 1,500 today. Set 0 to disable. Default 0.",
    )
    p.add_argument(
        "--loss-limit",
        type=float,
        default=None,
        help="Halt new entries once day's realized LOSS reaches this Rs amount "
             "(absolute, no percentage). Example: --loss-limit 1200 stops after "
             "-Rs 1,200 loss. Overrides --daily-loss-cap if both set.",
    )
    return p.parse_args()


def main():
    args = parse_args()

    # Wire CLI flag → dry-run flag
    auto_on = (args.auto_orders == "on")
    OPTIONS_EXECUTION.dry_run = not auto_on

    # Small-capital mode tweaks
    if args.small_capital:
        OPTIONS_RISK.small_capital_mode = True
        OPTIONS_RISK.force_one_lot = True
        # Default daily loss cap to 60% in small mode (one bad trade often
        # wipes ~50% of capital — at 60% halt, you preserve ~40% for tomorrow)
        if args.daily_loss_cap is None:
            OPTIONS_RISK.daily_loss_limit_pct = 0.60
        OPTIONS_RISK.max_open_positions = 1   # only one trade at a time
        # Cap at 3 trades/day to prevent overtrading after losses.
        # With ₹1k cap and -60% catastrophic stop, 2-3 bad trades wipe everything.
        OPTIONS_RISK.max_trades_per_day = 3
    if args.daily_loss_cap is not None:
        OPTIONS_RISK.daily_loss_limit_pct = float(args.daily_loss_cap)
    if args.quick_profit is not None:
        OPTIONS_RISK.quick_profit_target_rs = float(args.quick_profit)
    if args.quick_profit_trail is not None:
        OPTIONS_RISK.quick_profit_trail_rs = max(0.0, float(args.quick_profit_trail))
    if args.quick_loss is not None:
        OPTIONS_RISK.quick_loss_limit_rs = float(args.quick_loss)
    if args.max_trades is not None:
        OPTIONS_RISK.max_trades_per_day = int(args.max_trades)
    if args.max_lots is not None:
        OPTIONS_RISK.max_lots = max(0, int(args.max_lots))
    if args.cooldown is not None:
        OPTIONS_RISK.cooldown_sec_after_exit = int(args.cooldown)
    if args.cooldown_loss is not None:
        OPTIONS_RISK.cooldown_sec_after_loss = int(args.cooldown_loss)
    # Ensure the loss cooldown is never *shorter* than the win cooldown — the
    # whole point is that a loss is at least as serious as a win.
    if OPTIONS_RISK.cooldown_sec_after_loss < OPTIONS_RISK.cooldown_sec_after_exit:
        OPTIONS_RISK.cooldown_sec_after_loss = OPTIONS_RISK.cooldown_sec_after_exit
    if args.adx_min is not None:
        OPTIONS_RISK.adx_min_threshold = float(args.adx_min)
    if args.skip_open_min is not None:
        OPTIONS_RISK.skip_open_minutes = max(0, int(args.skip_open_min))
    if args.min_score is not None:
        OPTIONS_RISK.min_score = max(0, int(args.min_score))
    if args.last_entry is not None:
        if args.last_entry.strip().lower() == "off":
            OPTIONS_RISK.last_entry_hhmm = None
        else:
            try:
                hh, mm = (int(x) for x in args.last_entry.split(":"))
                OPTIONS_RISK.last_entry_hhmm = (hh, mm)
            except (ValueError, TypeError):
                log.warning(f"Bad --last-entry '{args.last_entry}' (want HH:MM) — "
                            f"keeping {OPTIONS_RISK.last_entry_hhmm}")
    if args.expiry_exit is not None:
        try:
            hh, mm = (int(x) for x in args.expiry_exit.split(":"))
            OPTIONS_RISK.expiry_day_exit_hhmm = (hh, mm)
        except (ValueError, TypeError):
            log.warning(f"Bad --expiry-exit '{args.expiry_exit}' (want HH:MM) — "
                        f"keeping {OPTIONS_RISK.expiry_day_exit_hhmm}")
    if args.rsi_bull_min is not None:
        OPTIONS_RISK.rsi_bull_min = float(args.rsi_bull_min)
    if args.rsi_bear_max is not None:
        OPTIONS_RISK.rsi_bear_max = float(args.rsi_bear_max)
    if args.max_premium_above_open is not None:
        # Flag is a percent (30); config stores a fraction (0.30). 0 disables.
        OPTIONS_RISK.max_premium_above_open_pct = max(0.0, float(args.max_premium_above_open) / 100.0)
    if args.all_indices and args.small_capital:
        OPTIONS_RISK.small_underlyings = ("SENSEX", "BANKNIFTY", "NIFTY", "FINNIFTY")
    if args.profit_target is not None:
        OPTIONS_RISK.daily_profit_target_rs = float(args.profit_target)
    if args.loss_limit is not None:
        OPTIONS_RISK.daily_loss_limit_rs = float(args.loss_limit)

    print()
    print("=" * 70)
    if auto_on:
        print("  MODE: LIVE TRADING -- real orders WILL be placed via Kite")
    else:
        print("  MODE: SIGNALS-ONLY -- Telegram alerts only, no real orders")
    if args.small_capital:
        print("  SUB-MODE: SMALL-CAPITAL -- SENSEX only, 1 lot, no SL risk math")
        print("            Catastrophic premium stop: -60%")
    print("=" * 70)
    print()

    # Build Kite client first so we can ask it for the balance
    log.info("Authenticating with Kite...")
    kite = make_kite()

    # Resolve capital: --capital override > live balance > config default
    if args.capital is not None:
        OPTIONS_RISK.capital = float(args.capital)
        log.info(f"Capital set via --capital flag: Rs {OPTIONS_RISK.capital:,.0f}")
    else:
        live_cash = fetch_available_cash(kite)
        if live_cash is None:
            log.warning(
                f"Could not fetch balance from Kite — falling back to "
                f"config default Rs {OPTIONS_RISK.capital:,.0f}"
            )
        elif live_cash <= 0:
            log.warning(
                f"Kite available cash = Rs {live_cash:,.0f}. "
                f"In LIVE mode, no orders will be placeable."
            )
            OPTIONS_RISK.capital = max(0.0, live_cash)
        else:
            OPTIONS_RISK.capital = live_cash
            log.info(f"Capital set from live Kite balance: Rs {OPTIONS_RISK.capital:,.0f}")

    # In small-capital mode, set the per-trade premium budget = 95% of capital
    # (leave a small buffer for brokerage/STT).
    if OPTIONS_RISK.small_capital_mode:
        OPTIONS_RISK.max_premium_cost_per_trade = OPTIONS_RISK.capital * 0.95
        log.info(
            f"Small-capital mode: max premium cost/trade = "
            f"Rs {OPTIONS_RISK.max_premium_cost_per_trade:.0f} "
            f"(95% of Rs {OPTIONS_RISK.capital:.0f})"
        )
    if OPTIONS_RISK.max_lots > 0:
        log.info(f"  Lot sizing: AUTO from capital, capped at {OPTIONS_RISK.max_lots} lots "
                 f"(stops scale per-lot)")
    else:
        log.info(f"  Lot sizing: AUTO from capital + live lot price (stops scale per-lot)")

    # Pre-flight check for live mode (skip for small-capital mode which expects low balance)
    if auto_on and not OPTIONS_RISK.small_capital_mode:
        min_required = 5000.0   # rough floor — below this even SENSEX 1 lot is risky
        if OPTIONS_RISK.capital < min_required:
            log.error(
                f"Available capital Rs {OPTIONS_RISK.capital:,.0f} is below "
                f"the practical minimum Rs {min_required:,.0f} for options. "
                f"Bot will run but most signals will result in 0-lot sizing "
                f"(can't afford a single lot within risk budget)."
            )
            send_telegram(
                f"⚠️ <b>Low balance warning</b>\n"
                f"Available: Rs {OPTIONS_RISK.capital:,.0f}\n"
                f"Most signals will not produce orders."
            )

    # Build & run the bot (it re-uses the kite client by re-auth — simple but
    # consistent with the existing constructor)
    bot = OptionsBot()
    bot.run()


if __name__ == "__main__":
    main()
