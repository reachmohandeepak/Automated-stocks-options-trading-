"""
options_signal.py
-----------------
Intraday options signal generator for NIFTY, BANKNIFTY and SENSEX.

Uses yfinance to pull the underlying index data, runs the same technical
indicators as the equity bot, and emits an actionable option trade idea:

   "BUY NIFTY 29-MAY-26 23650 CE  |  Spot: 23648  |  Stop if spot < 23550"

The strategy decides DIRECTION (call vs put). Strike selection uses the
ATM convention (closest standard strike to spot). Expiry is the next
weekly expiry for the underlying.

What this CANNOT tell you (without option chain data):
   * Live option premium    →  look up in your broker terminal
   * Implied volatility     →  same
   * Greeks                 →  same
   * Premium-based stop     →  we give a SPOT-based stop instead; convert
                               using delta ~ 0.5 for ATM options

Run anytime:   python options_signal.py
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

warnings.filterwarnings("ignore")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import pandas as pd
import pytz
import yfinance as yf

import indicators as ind

IST = pytz.timezone("Asia/Kolkata")


# ============================================================================
# Underlying configuration
# ============================================================================
@dataclass
class Underlying:
    name: str               # e.g. "NIFTY"
    yf_symbol: str          # yfinance ticker, e.g. "^NSEI"
    strike_step: int        # spacing between strikes (50 for NIFTY, 100 BANKNIFTY/SENSEX)
    lot_size: int           # current SEBI lot size
    weekly_expiry_weekday: Optional[int]   # 0=Mon, 1=Tue, ... 6=Sun; None = monthly only
    expiry_label: str       # human-readable expiry cadence
    exchange: str           # NSE / BSE


# Current as of 2026 — SEBI consolidated weekly expiries to one per exchange.
UNDERLYINGS = [
    Underlying("NIFTY",     "^NSEI",    50,  75, 3, "Weekly (Thursday)",  "NSE"),
    Underlying("BANKNIFTY", "^NSEBANK", 100, 30, None, "Monthly (last Thursday)", "NSE"),
    Underlying("SENSEX",    "^BSESN",   100, 20, 1, "Weekly (Tuesday)",   "BSE"),
    Underlying("FINNIFTY",  "^CNXFIN",  50,  65, None, "Monthly (last Tuesday)", "NSE"),
]


# ============================================================================
# Expiry helpers
# ============================================================================
def next_weekly_expiry(weekday: int) -> datetime:
    """Next occurrence of `weekday` (0=Mon...6=Sun) in IST. If today IS that
    weekday and market hasn't closed yet, return today; else go to next week.
    """
    now = datetime.now(IST)
    days_ahead = (weekday - now.weekday()) % 7
    if days_ahead == 0 and now.time() >= datetime.strptime("15:30", "%H:%M").time():
        days_ahead = 7
    return (now + timedelta(days=days_ahead)).replace(hour=15, minute=30, second=0, microsecond=0)


def last_thursday_of_month(ref: datetime) -> datetime:
    """Last Thursday of the same month as `ref` (used for monthly expiry)."""
    # Walk to first day of next month, then back to previous Thursday
    if ref.month == 12:
        next_month = ref.replace(year=ref.year + 1, month=1, day=1)
    else:
        next_month = ref.replace(month=ref.month + 1, day=1)
    last_day = next_month - timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7  # 3 = Thursday
    return last_day - timedelta(days=offset)


def next_monthly_expiry() -> datetime:
    """Next monthly expiry = last Thursday of current month (or next month if past)."""
    now = datetime.now(IST)
    this_month = last_thursday_of_month(now)
    if this_month.date() < now.date():
        # Already past — use next month's last Thursday
        nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
        return last_thursday_of_month(nxt)
    return this_month


def expiry_for(u: Underlying) -> datetime:
    if u.weekly_expiry_weekday is not None:
        return next_weekly_expiry(u.weekly_expiry_weekday)
    return next_monthly_expiry()


# ============================================================================
# Strike selection — round spot to nearest standard strike
# ============================================================================
def atm_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


# ============================================================================
# Signal generation — bullish / bearish / neutral on the underlying
# ============================================================================
@dataclass
class OptionsSignal:
    underlying: Underlying
    direction: str          # "BUY_CALL" | "BUY_PUT" | "NO_TRADE"
    spot: float
    strike: int
    expiry: datetime
    stop_spot: float        # SL expressed as spot price
    target_spot: float
    reason: str
    score: int              # number of confirmations (0-5)
    indicators: dict        # snapshot for display


def generate_signal(u: Underlying) -> OptionsSignal:
    """Pull 5-min candles for the index, compute indicators, score the setup."""
    df = yf.Ticker(u.yf_symbol).history(period="60d", interval="5m", auto_adjust=False)
    if df is None or df.empty:
        return OptionsSignal(u, "NO_TRADE", 0, 0, expiry_for(u), 0, 0,
                             "no data from yfinance", 0, {})

    # Normalize to our indicator pipeline's expected schema
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    df = df.tail(300)

    enriched = ind.compute_all(df)
    snap = ind.latest_snapshot(enriched)
    if not snap or "close" not in snap:
        return OptionsSignal(u, "NO_TRADE", 0, 0, expiry_for(u), 0, 0,
                             "indicators not warmed up", 0, {})

    spot = snap["close"]
    rsi = snap.get("rsi", 50)
    macd = snap.get("macd", 0)
    macd_sig = snap.get("macd_signal", 0)
    ema9 = snap.get("ema9", spot)
    ema21 = snap.get("ema21", spot)
    ema50 = snap.get("ema50", spot)
    vwap = snap.get("vwap", spot)
    st_dir = snap.get("supertrend_dir", 0)

    # ----------------- Score bullish vs bearish confirmations -----------------
    bull_score = 0
    bear_score = 0
    bull_reasons = []
    bear_reasons = []

    # 1. Price vs VWAP
    if spot > vwap:
        bull_score += 1
        bull_reasons.append(f"spot>{vwap:.0f} (VWAP)")
    elif spot < vwap:
        bear_score += 1
        bear_reasons.append(f"spot<{vwap:.0f} (VWAP)")

    # 2. EMA alignment
    if ema9 > ema21 > ema50:
        bull_score += 1
        bull_reasons.append("EMA 9>21>50")
    elif ema9 < ema21 < ema50:
        bear_score += 1
        bear_reasons.append("EMA 9<21<50")

    # 3. RSI in momentum zone
    if 50 < rsi < 70:
        bull_score += 1
        bull_reasons.append(f"RSI {rsi:.0f} bullish")
    elif 30 < rsi < 50:
        bear_score += 1
        bear_reasons.append(f"RSI {rsi:.0f} bearish")
    elif rsi >= 70:
        bear_reasons.append(f"RSI {rsi:.0f} overbought — caution")
    elif rsi <= 30:
        bull_reasons.append(f"RSI {rsi:.0f} oversold — possible reversal")

    # 4. MACD alignment
    if macd > macd_sig and macd > 0:
        bull_score += 1
        bull_reasons.append("MACD>signal & >0")
    elif macd < macd_sig and macd < 0:
        bear_score += 1
        bear_reasons.append("MACD<signal & <0")

    # 5. Supertrend
    if st_dir == 1:
        bull_score += 1
        bull_reasons.append("Supertrend bull")
    elif st_dir == -1:
        bear_score += 1
        bear_reasons.append("Supertrend bear")

    # ----------------- Decide direction (need 4/5 confirmations) -----------------
    threshold = 4
    expiry = expiry_for(u)
    strike = atm_strike(spot, u.strike_step)

    if bull_score >= threshold and bull_score > bear_score:
        # Stop = 0.4% below spot (rough rule for index options intraday)
        stop_spot = spot * 0.996
        target_spot = spot * 1.008   # 2x reward to risk
        return OptionsSignal(u, "BUY_CALL", spot, strike, expiry,
                             stop_spot, target_spot,
                             " + ".join(bull_reasons), bull_score, snap)

    if bear_score >= threshold and bear_score > bull_score:
        stop_spot = spot * 1.004
        target_spot = spot * 0.992
        return OptionsSignal(u, "BUY_PUT", spot, strike, expiry,
                             stop_spot, target_spot,
                             " + ".join(bear_reasons), bear_score, snap)

    # Neutral / mixed — no trade
    reason = (f"mixed signals (bull={bull_score}/5, bear={bear_score}/5) — "
              "wait for clearer setup")
    return OptionsSignal(u, "NO_TRADE", spot, strike, expiry, 0, 0,
                         reason, max(bull_score, bear_score), snap)


# ============================================================================
# Display
# ============================================================================
def render(sig: OptionsSignal) -> None:
    u = sig.underlying
    print()
    print("─" * 78)
    print(f"  {u.name}  ({u.exchange})")
    print("─" * 78)
    print(f"  Spot          :  {sig.spot:,.2f}")
    print(f"  ATM strike    :  {sig.strike:,}   (strike spacing = {u.strike_step})")
    print(f"  Expiry        :  {sig.expiry.strftime('%d-%b-%Y (%a)')}   "
          f"[{u.expiry_label}]")
    print(f"  Lot size      :  {u.lot_size}   "
          f"({u.lot_size} × premium = ₹ commitment per lot)")

    snap = sig.indicators
    if snap:
        print(f"  Indicators    :  RSI {snap.get('rsi', 0):.1f}   "
              f"MACD {snap.get('macd', 0):.2f}   "
              f"VWAP {snap.get('vwap', 0):,.2f}   "
              f"ST {'BULL' if snap.get('supertrend_dir')==1 else 'BEAR' if snap.get('supertrend_dir')==-1 else '-'}")

    print()
    if sig.direction == "BUY_CALL":
        contract = f"{u.name} {sig.expiry.strftime('%d%b%y').upper()} {sig.strike} CE"
        print(f"  >>> SIGNAL  :  BUY CALL  ({sig.score}/5 confirmations)")
        print(f"  >>> CONTRACT:  {contract}")
        print(f"  >>> ENTRY   :  At market (check option premium in broker terminal)")
        print(f"  >>> STOP    :  Exit if spot drops below {sig.stop_spot:,.2f}")
        print(f"                  (≈ {((sig.stop_spot/sig.spot)-1)*100:.2f}% from current spot)")
        print(f"  >>> TARGET  :  Spot {sig.target_spot:,.2f}  "
              f"(+{((sig.target_spot/sig.spot)-1)*100:.2f}% — gives ~2:1 RR)")
        print(f"  >>> REASON  :  {sig.reason}")
    elif sig.direction == "BUY_PUT":
        contract = f"{u.name} {sig.expiry.strftime('%d%b%y').upper()} {sig.strike} PE"
        print(f"  >>> SIGNAL  :  BUY PUT  ({sig.score}/5 confirmations)")
        print(f"  >>> CONTRACT:  {contract}")
        print(f"  >>> ENTRY   :  At market (check option premium in broker terminal)")
        print(f"  >>> STOP    :  Exit if spot rises above {sig.stop_spot:,.2f}")
        print(f"                  (≈ +{((sig.stop_spot/sig.spot)-1)*100:.2f}% from current spot)")
        print(f"  >>> TARGET  :  Spot {sig.target_spot:,.2f}  "
              f"({((sig.target_spot/sig.spot)-1)*100:.2f}% — gives ~2:1 RR)")
        print(f"  >>> REASON  :  {sig.reason}")
    else:
        print(f"  >>> SIGNAL  :  NO TRADE  (best confirmations: {sig.score}/5)")
        print(f"  >>> REASON  :  {sig.reason}")


def main():
    print()
    print("=" * 78)
    print(f"  OPTIONS INTRADAY SIGNALS  —  yfinance feed  —  "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)
    print(f"  Data lag : ~15 min delayed during market hours")
    print(f"  Strategy : 5-min candle scoring  —  needs 4 of 5 confirmations to fire")
    print(f"  Confirms : (1) Spot vs VWAP   (2) EMA 9/21/50 alignment   "
          f"(3) RSI zone   (4) MACD   (5) Supertrend")

    for u in UNDERLYINGS:
        try:
            sig = generate_signal(u)
            render(sig)
        except Exception as e:
            print(f"\n  {u.name}: error — {e}")

    print()
    print("=" * 78)
    print("  REMINDERS")
    print("=" * 78)
    print("  * Options expire — never hold past expiry, theta will gut the premium.")
    print("  * 'Stop on spot' translates to premium loss via delta (ATM ~ 0.5).")
    print("    Example: spot drops 50 pts on a 50-strike-step index → CE loses ~₹25.")
    print("  * Lot sizes change occasionally — verify with broker before trading.")
    print("  * yfinance is 15-min delayed: real action may have already moved.")
    print("  * SEBI capital requirements: ensure your account can take the lot's MTM.")
    print()


if __name__ == "__main__":
    main()
