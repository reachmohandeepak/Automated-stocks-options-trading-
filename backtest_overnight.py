"""
backtest_overnight.py
---------------------
Validates the OVERNIGHT-GAP options-BUY strategy BEFORE we build a live bot.

Strategy under test:
  * Signal on DAILY candles (EMA/RSI/MACD/Supertrend vote) at the close.
  * Bullish -> buy ATM CALL at close;  Bearish -> buy ATM PUT at close.
  * Exit at the NEXT session's OPEN (one overnight hold).

Premium model (delta-approximation + overnight theta):
  entry_premium = ATM_PCT * spot
  exit_premium  = entry + 0.5 * (next_open - close) * dir  -  theta_one_night
The whole question is: does the overnight GAP (in the signal's direction) beat
ONE NIGHT OF THETA? Theta is the big unknown, so we SWEEP it.

Standalone — does NOT import or modify options_bot.py.
Run:  python backtest_overnight.py
"""
from __future__ import annotations
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import yfinance as yf
import indicators as ind

ATM_PCT = 0.008            # ATM weekly premium ~ 0.8% of spot
DELTA = 0.5
COST_RT = 0.006            # round-trip brokerage+slippage on premium

UNDERLYINGS = [
    ("NIFTY",     "^NSEI",     75),
    ("BANKNIFTY", "^NSEBANK",  30),
    ("SENSEX",    "^BSESN",    20),
]


def daily_signal(row) -> str:
    """EMA/RSI/MACD/ST vote on a daily bar -> BUY_CALL / BUY_PUT / NO_TRADE."""
    req = ("ema9", "ema21", "ema50", "rsi", "macd", "macd_signal", "supertrend_dir")
    if any(pd.isna(row[c]) for c in req):
        return "NO_TRADE"
    bull = bear = 0
    e9, e21, e50 = row["ema9"], row["ema21"], row["ema50"]
    if e9 > e21 > e50: bull += 1
    elif e9 < e21 < e50: bear += 1
    r = row["rsi"]
    if 55 < r < 75: bull += 1
    elif 25 < r < 45: bear += 1
    if row["macd"] > row["macd_signal"] and row["macd"] > 0: bull += 1
    elif row["macd"] < row["macd_signal"] and row["macd"] < 0: bear += 1
    if row["supertrend_dir"] == 1: bull += 1
    elif row["supertrend_dir"] == -1: bear += 1
    if bull >= 3 and bull > bear: return "BUY_CALL"
    if bear >= 3 and bear > bull: return "BUY_PUT"
    return "NO_TRADE"


def run(name, yf_symbol, lot, theta_pct):
    df = yf.Ticker(yf_symbol).history(period="1y", interval="1d", auto_adjust=False)
    if df is None or len(df) < 60:
        return None
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = ind.compute_all(df)
    trades = []
    for i in range(55, len(df) - 1):
        sig = daily_signal(df.iloc[i])
        if sig == "NO_TRADE":
            continue
        close = float(df.iloc[i]["close"])
        nxt_open = float(df.iloc[i + 1]["open"])
        entry_prem = close * ATM_PCT
        dir_mult = 1 if sig == "BUY_CALL" else -1
        intrinsic = DELTA * (nxt_open - close) * dir_mult
        theta = entry_prem * theta_pct
        exit_prem = max(entry_prem + intrinsic - theta, 0.0)
        gross = (exit_prem - entry_prem) * lot
        cost = (entry_prem + exit_prem) * lot * (COST_RT / 2)
        trades.append(gross - cost)
    return trades


def summarize(label, trades):
    if not trades:
        print(f"  {label:<28} NO TRADES"); return
    arr = np.array(trades)
    wins = (arr > 0).sum()
    total = arr.sum()
    pf_den = -arr[arr <= 0].sum()
    pf = (arr[arr > 0].sum() / pf_den) if pf_den else float("inf")
    pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
    print(f"  {label:<28}{len(arr):>5} trades  win {wins/len(arr)*100:>4.0f}%  "
          f"net Rs {total:>+8,.0f}  PF {pfs}")


def main():
    print("\n" + "=" * 78)
    print("  OVERNIGHT-GAP BUY BACKTEST  —  1-year daily candles, 1 lot/trade")
    print("=" * 78)
    print(f"  Model: ATM premium = {ATM_PCT*100:.1f}% of spot | delta {DELTA} | "
          f"round-trip cost {COST_RT*100:.1f}%")
    print("  Sweeping the OVERNIGHT THETA assumption (the key unknown):\n")

    data = {}
    for name, sym, lot in UNDERLYINGS:
        # cache raw fetch via a 0-theta run reused per theta below by refetching
        data[name] = (sym, lot)

    for theta_pct in (0.00, 0.02, 0.04, 0.06, 0.08):
        print("-" * 78)
        print(f"  THETA = {theta_pct*100:.0f}% of premium per night")
        print("-" * 78)
        all_t = []
        for name, (sym, lot) in data.items():
            t = run(name, sym, lot, theta_pct)
            if t is not None:
                summarize(name, t)
                all_t.extend(t)
        summarize("ALL", all_t)
        print()

    print("  READING IT:")
    print("   * theta 0%   = pure gap edge (unrealistic best case).")
    print("   * theta 4-6% = realistic for a weekly ATM option held one night.")
    print("   * If 'ALL' is negative at realistic theta, overnight BUYING has no")
    print("     edge — the gap doesn't pay for the decay. Don't build the live bot.")
    print()


if __name__ == "__main__":
    main()
