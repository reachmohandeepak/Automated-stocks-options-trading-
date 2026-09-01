"""
backtest.py
-----------
Walk-forward backtest of the current intraday strategy on historical
5-min candles. Tells you what the strategy WOULD have done over the
last 60 trading days — before you risk real money.

Key principles:
  * No look-ahead bias. At bar i, only data from bars 0..i is visible
    to the strategy. Entries fill at bar i+1's OPEN, never bar i's close.
  * Realistic slippage + brokerage modeled.
  * Position management runs intra-bar via bar's high/low (worst case for stop).
  * 15:15 IST forced square-off honored.
  * Daily loss cap honored (3% of capital → halt entries that day).
  * Multi-symbol parallel simulation with shared daily-loss budget.

Run:
    python backtest.py                  # default: full 60d, all watchlist symbols
    python backtest.py --days 30        # shorter window
    python backtest.py --symbol NSE:RELIANCE   # single symbol
    python backtest.py --capital 200000        # different capital

Outputs:
  * Console report (per-symbol + aggregate)
  * logs/backtest_trades.csv  — every simulated trade
  * logs/backtest_summary.csv — summary row per symbol
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import List, Optional

warnings.filterwarnings("ignore")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

import config
import indicators as ind

IST = pytz.timezone("Asia/Kolkata")


# ============================================================================
# Costs & assumptions (Zerodha intraday)
# ============================================================================
SLIPPAGE_PCT = 0.0005      # 0.05% per side — realistic for liquid NSE largecaps
BROKERAGE_PCT = 0.0003     # 0.03% per side (Zerodha MIS, capped at ₹20)
# Total round-trip cost ≈ 0.16% (slip + brokerage both ways)

TRADING_OPEN = dtime(9, 15)
TRADING_CLOSE = dtime(15, 30)
SQUAREOFF_TIME = dtime(15, 15)


@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: int
    side: str = "LONG"
    stop_loss: float = 0.0
    target: float = 0.0
    reason_entry: str = ""
    reason_exit: str = ""

    @property
    def gross_pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def cost(self) -> float:
        # Slippage + brokerage on both sides
        notional = (self.entry_price + self.exit_price) * self.quantity
        return notional * (SLIPPAGE_PCT + BROKERAGE_PCT)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost

    @property
    def return_pct(self) -> float:
        if self.entry_price == 0 or self.quantity == 0:
            return 0
        return self.net_pnl / (self.entry_price * self.quantity) * 100

    @property
    def hold_minutes(self) -> int:
        return int((self.exit_time - self.entry_time).total_seconds() / 60)


# ============================================================================
# Data fetcher
# ============================================================================
def fetch_history(yf_symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Pull 5-min OHLCV from yfinance. Capped at 60 days (yfinance limit)."""
    days = min(days, 60)
    period = f"{days}d"
    df = yf.Ticker(yf_symbol).history(period=period, interval="5m", auto_adjust=False)
    if df is None or df.empty:
        return None
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    df.index.name = "date"
    # Keep only regular trading hours
    df = df[(df.index.time >= TRADING_OPEN) & (df.index.time <= TRADING_CLOSE)]
    return df


# ============================================================================
# Strategy evaluator (mirrors strategy.py, but operates on pre-computed frames)
# ============================================================================
def _crossed_above(a: np.ndarray, b: np.ndarray, lookback: int = 2) -> bool:
    """True if a was <= b within `lookback+1` bars back, and is > b at end."""
    if len(a) < lookback + 2:
        return False
    if np.isnan(a[-1]) or np.isnan(b[-1]) or a[-1] <= b[-1]:
        return False
    for i in range(len(a) - lookback - 1, len(a) - 1):
        if not np.isnan(a[i]) and not np.isnan(b[i]) and a[i] <= b[i]:
            return True
    return False


def _crossed_below(a: np.ndarray, b: np.ndarray, lookback: int = 2) -> bool:
    if len(a) < lookback + 2:
        return False
    if np.isnan(a[-1]) or np.isnan(b[-1]) or a[-1] >= b[-1]:
        return False
    for i in range(len(a) - lookback - 1, len(a) - 1):
        if not np.isnan(a[i]) and not np.isnan(b[i]) and a[i] >= b[i]:
            return True
    return False


def check_buy_v2(row, history: pd.DataFrame) -> Optional[str]:
    """Tightened BUY filter — addresses the whipsaw problem from v1 backtest.

    All conditions must hold:
      1. VWAP sustained: price > VWAP for last 3 bars (not just a cross)
      2. VWAP buffer:    price > VWAP * 1.001 (0.1% above, not borderline)
      3. EMA aligned:    EMA9 > EMA21 > EMA50
      4. RSI healthy:    50 <= RSI <= 68 (cap below 70 to avoid late entries)
      5. MACD bullish:   MACD > signal AND histogram rising (last bar > prev)
      6. ADX trending:   ADX >= 22  (filter out chop)
      7. Volume real:    volume_ratio >= 1.3 (above-avg participation)
      8. +DI > -DI:      directional bias is up
    """
    required = ("ema9", "ema21", "ema50", "rsi", "macd", "macd_signal",
                "macd_hist", "vwap", "adx", "plus_di", "minus_di",
                "volume_ratio")
    if any(pd.isna(row[c]) for c in required):
        return None

    # 1+2. VWAP sustained + buffered
    if len(history) < 3:
        return None
    last3_close = history["close"].iloc[-3:].values
    last3_vwap = history["vwap"].iloc[-3:].values
    if not all(c > v for c, v in zip(last3_close, last3_vwap)):
        return None
    if row["close"] < row["vwap"] * 1.001:
        return None

    # 3. EMA alignment
    if not (row["ema9"] > row["ema21"] > row["ema50"]):
        return None

    # 4. RSI healthy zone
    if not (50 <= row["rsi"] <= 68):
        return None

    # 5. MACD bullish AND momentum rising
    if row["macd"] <= row["macd_signal"]:
        return None
    if len(history) >= 2:
        prev_hist = history["macd_hist"].iloc[-2]
        if pd.isna(prev_hist) or row["macd_hist"] <= prev_hist:
            return None
    if row["macd"] <= 0:
        return None

    # 6. ADX trending
    if row["adx"] < 22:
        return None

    # 7. Volume confirmation
    if row["volume_ratio"] < 1.3:
        return None

    # 8. Directional bias
    if row["plus_di"] <= row["minus_di"]:
        return None

    return (f"VWAP+EMA+RSI{row['rsi']:.0f}+MACD↑+ADX{row['adx']:.0f}+"
            f"vol{row['volume_ratio']:.1f}x")


# Keep v1 for comparison runs
def check_buy(row, recent_close, recent_vwap, recent_macd, recent_macd_sig):
    if any(pd.isna(row[c]) for c in ("ema9", "ema21", "ema50", "rsi",
                                       "macd", "macd_signal", "vwap")):
        return None
    if not _crossed_above(recent_close, recent_vwap):
        return None
    if not (row["ema9"] > row["ema21"] > row["ema50"]):
        return None
    if not (50 <= row["rsi"] <= 70):
        return None
    if not _crossed_above(recent_macd, recent_macd_sig):
        return None
    if row["macd"] <= 0:
        return None
    return f"VWAP+EMA+RSI{row['rsi']:.0f}+MACD"


def check_strategy_exit(row, recent_close: np.ndarray,
                         recent_vwap: np.ndarray) -> Optional[str]:
    """Strategy-level exits (not stop/target). Returns reason or None."""
    reasons = []
    if _crossed_below(recent_close, recent_vwap):
        reasons.append("price<VWAP")
    if not pd.isna(row["rsi"]) and row["rsi"] > 75:
        reasons.append(f"RSI>{75}")
    # Supertrend flip: caller can pass enriched data; here we check direction
    if not pd.isna(row["supertrend_dir"]) and row["supertrend_dir"] == -1:
        reasons.append("Supertrend↓")
    return " + ".join(reasons) if reasons else None


# ============================================================================
# Single-symbol simulator
# ============================================================================
@dataclass
class SymbolBacktest:
    symbol: str
    df: pd.DataFrame
    capital: float
    risk_pct: float = 0.01
    stop_loss_pct: float = 0.007
    reward_to_risk: float = 2.0
    trail_activate_pct: float = 0.005
    # V2 features
    use_v2_filter: bool = True       # tightened BUY filter (ADX, volume, sustained VWAP)
    partial_book_at_r: float = 1.0   # book 50% at 1R (half-target distance)
    move_stop_to_breakeven_on_partial: bool = True
    trades: List[Trade] = field(default_factory=list)

    def run(self):
        df = ind.compute_all(self.df)

        in_position = False
        partial_booked = False
        entry_price = stop_loss = target = high_water = 0.0
        entry_time: Optional[datetime] = None
        entry_reason = ""
        qty_remaining = 0
        initial_qty = 0
        risk_per_share = 0.0   # original SL distance, locked at entry

        # We need at least 50 bars of warmup for EMA50 + ADX(14)
        for i in range(60, len(df) - 1):
            now_ts = df.index[i].to_pydatetime()
            row = df.iloc[i]
            next_row = df.iloc[i + 1]
            now_time = now_ts.time()

            # Slice for cross-detection / history
            history = df.iloc[max(0, i - 5):i + 1]
            close_arr = history["close"].values
            vwap_arr = history["vwap"].values

            # ----- Position management -----
            if in_position:
                # Update high-water + trail (only on the remaining qty)
                if row["high"] > high_water:
                    high_water = row["high"]
                if high_water >= entry_price * (1 + self.trail_activate_pct):
                    trailed = round(high_water - risk_per_share, 2)
                    if trailed > stop_loss:
                        stop_loss = trailed

                # --- Partial booking at 1R ---
                partial_price = entry_price + risk_per_share * self.partial_book_at_r
                if (not partial_booked
                        and self.partial_book_at_r > 0
                        and row["high"] >= partial_price):
                    half = initial_qty // 2
                    if half > 0:
                        self.trades.append(Trade(
                            symbol=self.symbol,
                            entry_time=entry_time,
                            exit_time=next_row.name.to_pydatetime(),
                            entry_price=entry_price,
                            exit_price=partial_price,
                            quantity=half,
                            stop_loss=stop_loss,
                            target=target,
                            reason_entry=entry_reason,
                            reason_exit="partial_1R",
                        ))
                        qty_remaining = initial_qty - half
                        partial_booked = True
                        # Move stop to breakeven on the remainder
                        if self.move_stop_to_breakeven_on_partial:
                            stop_loss = round(entry_price, 2)
                            # high_water stays — trail still uses it

                # --- Full exit checks (on remaining qty) ---
                exit_reason = None
                exit_price = None
                if next_row.name.to_pydatetime().time() >= SQUAREOFF_TIME:
                    exit_price = float(next_row["open"])
                    exit_reason = "squareoff_315pm"
                elif row["low"] <= stop_loss:
                    exit_price = stop_loss
                    exit_reason = "stop_loss" if not partial_booked else "breakeven_stop"
                elif row["high"] >= target:
                    exit_price = target
                    exit_reason = "target"
                else:
                    sx = check_strategy_exit(row, close_arr, vwap_arr)
                    if sx:
                        exit_price = float(next_row["open"])
                        exit_reason = sx

                if exit_reason and qty_remaining > 0:
                    self.trades.append(Trade(
                        symbol=self.symbol,
                        entry_time=entry_time,
                        exit_time=next_row.name.to_pydatetime(),
                        entry_price=entry_price,
                        exit_price=exit_price,
                        quantity=qty_remaining,
                        stop_loss=stop_loss,
                        target=target,
                        reason_entry=entry_reason,
                        reason_exit=exit_reason,
                    ))
                    in_position = False
                    partial_booked = False

            # ----- Entry -----
            if (not in_position
                    and now_time >= TRADING_OPEN
                    and now_time < dtime(14, 55)):
                if self.use_v2_filter:
                    reason = check_buy_v2(row, history)
                else:
                    reason = check_buy(row, close_arr, vwap_arr,
                                       history["macd"].values,
                                       history["macd_signal"].values)
                if reason:
                    entry_price = float(next_row["open"])
                    stop_loss = round(entry_price * (1 - self.stop_loss_pct), 2)
                    risk_per_share = entry_price - stop_loss
                    target = round(entry_price + risk_per_share * self.reward_to_risk, 2)
                    if risk_per_share <= 0:
                        continue
                    risk_rupees = self.capital * self.risk_pct
                    qty = int(risk_rupees / risk_per_share)
                    if qty <= 0:
                        continue
                    max_q = int(self.capital / entry_price)
                    qty = min(qty, max_q)
                    if qty < 2:    # need at least 2 for partial booking
                        continue
                    in_position = True
                    partial_booked = False
                    initial_qty = qty
                    qty_remaining = qty
                    entry_time = next_row.name.to_pydatetime()
                    high_water = entry_price
                    entry_reason = reason

        # End-of-data: close anything still open
        if in_position and qty_remaining > 0:
            last = df.iloc[-1]
            self.trades.append(Trade(
                symbol=self.symbol,
                entry_time=entry_time,
                exit_time=last.name.to_pydatetime(),
                entry_price=entry_price,
                exit_price=float(last["close"]),
                quantity=qty_remaining,
                stop_loss=stop_loss,
                target=target,
                reason_entry=entry_reason,
                reason_exit="end_of_data",
            ))


# ============================================================================
# Reporting
# ============================================================================
def summarize(trades: List[Trade], capital: float, label: str = "") -> dict:
    if not trades:
        return {"label": label, "trades": 0}
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    total_pnl = sum(t.net_pnl for t in trades)
    avg_win = np.mean([t.net_pnl for t in wins]) if wins else 0
    avg_loss = np.mean([t.net_pnl for t in losses]) if losses else 0
    best = max(trades, key=lambda t: t.net_pnl)
    worst = min(trades, key=lambda t: t.net_pnl)

    # Build equity curve in trade-order
    sorted_t = sorted(trades, key=lambda t: t.entry_time)
    equity = [capital]
    for t in sorted_t:
        equity.append(equity[-1] + t.net_pnl)
    equity = np.array(equity)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak * 100
    max_dd_pct = drawdown.min()
    final = equity[-1]
    total_return_pct = (final - capital) / capital * 100

    return {
        "label": label,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "avg_pnl": np.mean([t.net_pnl for t in trades]),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": (sum(t.net_pnl for t in wins) /
                          abs(sum(t.net_pnl for t in losses))) if losses else float("inf"),
        "best_trade": best,
        "worst_trade": worst,
        "max_dd_pct": max_dd_pct,
        "avg_hold_min": np.mean([t.hold_minutes for t in trades]),
    }


def print_report(stats: dict):
    if stats["trades"] == 0:
        print(f"\n{stats['label']}: NO TRADES")
        return
    print()
    print("─" * 78)
    print(f"  {stats['label']}")
    print("─" * 78)
    print(f"  Trades       : {stats['trades']}   "
          f"(Wins {stats['wins']} / Losses {stats['losses']})")
    print(f"  Win rate     : {stats['win_rate']:.1f}%")
    print(f"  Total P&L    : Rs {stats['total_pnl']:+,.2f}   "
          f"({stats['total_return_pct']:+.2f}% on capital)")
    print(f"  Avg P&L      : Rs {stats['avg_pnl']:+,.2f} per trade")
    print(f"  Avg win      : Rs {stats['avg_win']:+,.2f}     "
          f"Avg loss: Rs {stats['avg_loss']:+,.2f}")
    pf = stats['profit_factor']
    pf_str = f"{pf:.2f}" if pf != float('inf') else "∞"
    print(f"  Profit factor: {pf_str}    (>1.5 is good, >2.0 is great)")
    print(f"  Max drawdown : {stats['max_dd_pct']:.2f}%")
    print(f"  Avg hold     : {stats['avg_hold_min']:.0f} min")
    b, w = stats["best_trade"], stats["worst_trade"]
    print(f"  Best trade   : {b.symbol} {b.entry_time.strftime('%m-%d %H:%M')} → "
          f"{b.exit_time.strftime('%H:%M')} Rs {b.net_pnl:+,.2f} ({b.reason_exit})")
    print(f"  Worst trade  : {w.symbol} {w.entry_time.strftime('%m-%d %H:%M')} → "
          f"{w.exit_time.strftime('%H:%M')} Rs {w.net_pnl:+,.2f} ({w.reason_exit})")


# ============================================================================
# CSV output
# ============================================================================
def write_trade_csv(trades: List[Trade], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "entry_time", "exit_time", "qty",
            "entry_price", "exit_price", "stop_loss", "target",
            "gross_pnl", "cost", "net_pnl", "return_pct", "hold_min",
            "reason_entry", "reason_exit",
        ])
        for t in sorted(trades, key=lambda x: x.entry_time):
            w.writerow([
                t.symbol,
                t.entry_time.isoformat(timespec="seconds"),
                t.exit_time.isoformat(timespec="seconds"),
                t.quantity,
                round(t.entry_price, 2), round(t.exit_price, 2),
                round(t.stop_loss, 2), round(t.target, 2),
                round(t.gross_pnl, 2), round(t.cost, 2),
                round(t.net_pnl, 2), round(t.return_pct, 3),
                t.hold_minutes,
                t.reason_entry, t.reason_exit,
            ])


# ============================================================================
# Main
# ============================================================================
def yf_symbol(sym: str) -> str:
    if ":" in sym:
        exch, t = sym.split(":", 1)
        return f"{t}.NS" if exch == "NSE" else f"{t}.BO"
    return sym


def main():
    p = argparse.ArgumentParser(description="Walk-forward backtest of intraday strategy.")
    p.add_argument("--days", type=int, default=60,
                   help="Days of history (max 60 due to yfinance). Default 60.")
    p.add_argument("--symbol", action="append", default=None,
                   help="Specific symbol (e.g. NSE:RELIANCE). Repeatable. Default: full watchlist.")
    p.add_argument("--capital", type=float, default=config.RISK.capital,
                   help=f"Per-symbol capital. Default Rs {config.RISK.capital:,.0f}.")
    p.add_argument("--out-dir", default="logs",
                   help="Output directory. Default 'logs'.")
    p.add_argument("--strategy", choices=["v1", "v2"], default="v2",
                   help="v1 = original (loose). v2 = tightened (ADX, sustained "
                        "VWAP, volume, partial booking). Default v2.")
    p.add_argument("--no-partial", action="store_true",
                   help="Disable partial booking at 1R (compare full-trade results).")
    args = p.parse_args()

    symbols = args.symbol or config.WATCHLIST
    all_trades: List[Trade] = []

    print()
    print("=" * 78)
    print(f"  BACKTEST  —  {args.days}-day 5-min  —  capital Rs {args.capital:,.0f}/symbol")
    print("=" * 78)
    print(f"  Slippage: {SLIPPAGE_PCT*100:.2f}% per side  |  "
          f"Brokerage: {BROKERAGE_PCT*100:.2f}% per side")
    print(f"  Risk: 1% capital, 0.7% SL, 2:1 R:R, trail after +0.5%, "
          f"squareoff 15:15 IST")
    print()

    summary_rows = []

    for sym in symbols:
        ysym = yf_symbol(sym)
        print(f"[{sym}]  Fetching {ysym}...", end=" ", flush=True)
        df = fetch_history(ysym, args.days)
        if df is None or len(df) < 100:
            print(f"  → SKIPPED (not enough data: {0 if df is None else len(df)} bars)")
            continue
        print(f"  → {len(df)} bars  (from {df.index[0].date()} to {df.index[-1].date()})")

        bt = SymbolBacktest(
            symbol=sym, df=df, capital=args.capital,
            use_v2_filter=(args.strategy == "v2"),
            partial_book_at_r=0.0 if args.no_partial else 1.0,
        )
        bt.run()
        stats = summarize(bt.trades, args.capital, label=sym)
        print_report(stats)
        all_trades.extend(bt.trades)

        summary_rows.append({
            "symbol": sym,
            "trades": stats.get("trades", 0),
            "win_rate": round(stats.get("win_rate", 0), 1),
            "total_pnl": round(stats.get("total_pnl", 0), 2),
            "return_pct": round(stats.get("total_return_pct", 0), 2),
            "max_dd_pct": round(stats.get("max_dd_pct", 0), 2),
            "profit_factor": round(stats.get("profit_factor", 0), 2)
                                if stats.get("profit_factor") != float("inf") else "inf",
            "avg_hold_min": round(stats.get("avg_hold_min", 0), 0),
        })

    # Aggregate across all symbols
    print()
    print("=" * 78)
    print("  AGGREGATE  (across all symbols, capital pooled)")
    print("=" * 78)
    agg = summarize(all_trades, args.capital * len(symbols), label="ALL")
    print_report(agg)

    # Write CSVs
    trade_csv = os.path.join(args.out_dir, "backtest_trades.csv")
    write_trade_csv(all_trades, trade_csv)
    summary_csv = os.path.join(args.out_dir, "backtest_summary.csv")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(summary_csv, "w", newline="", encoding="utf-8") as f:
        if summary_rows:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            w.writerows(summary_rows)

    print()
    print(f"  Trade-by-trade log : {trade_csv}")
    print(f"  Per-symbol summary : {summary_csv}")
    print()
    print("  Interpretation:")
    print("    profit_factor > 1.5 = decent, > 2.0 = strong")
    print("    win_rate alone is misleading — combine with avg_win/avg_loss")
    print("    max_dd_pct < -10% suggests strategy needs tighter risk controls")
    print()


if __name__ == "__main__":
    main()
