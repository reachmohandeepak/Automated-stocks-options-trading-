"""
intraday_snapshot.py
--------------------
One-shot intraday snapshot using yfinance — fetches today's OHLCV for
the configured watchlist, computes all indicators, evaluates the strategy,
and prints a readable per-symbol report.

Run anytime:   python intraday_snapshot.py
"""

import sys
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")  # silence yfinance/pandas chatter

# Force UTF-8 on Windows console
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import config
import indicators as ind
from data_feed import YfinanceDataFeed
from strategy import SignalType, evaluate


def fmt(v, nd=2, na="—"):
    if v is None:
        return na
    try:
        return f"{v:.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def main() -> None:
    print()
    print("=" * 78)
    print(f"  INTRADAY SNAPSHOT  —  yfinance feed  —  "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)
    print(f"  Watchlist        : {', '.join(config.WATCHLIST)}")
    print(f"  Primary interval : {config.TIMING.primary_interval_min}-min candles")
    print(f"  Data lag         : ~15 min delayed during NSE market hours")
    print("=" * 78)

    feed = YfinanceDataFeed(config.WATCHLIST)
    feed.resolve_tokens()
    print("\nFetching from yfinance...\n")
    feed.bootstrap_history()

    primary = config.TIMING.primary_interval_min
    summary = []

    for sym in config.WATCHLIST:
        df = feed.get_candles(sym, primary)
        if df is None or df.empty:
            print(f"\n{sym}  —  NO DATA")
            continue

        enriched = ind.compute_all(df)
        snap = ind.latest_snapshot(enriched)
        sig = evaluate(enriched, sym, has_open_position=False)

        # Today's session OHL
        today = enriched.index[-1].date()
        today_df = enriched[enriched.index.date == today]
        if not today_df.empty:
            day_o = today_df["open"].iloc[0]
            day_h = today_df["high"].max()
            day_l = today_df["low"].min()
            day_c = today_df["close"].iloc[-1]
            day_chg = day_c - day_o
            day_chg_pct = (day_chg / day_o) * 100 if day_o else 0
        else:
            day_o = day_h = day_l = day_c = day_chg = day_chg_pct = None

        st_dir = snap.get("supertrend_dir", 0)
        st_label = "BULL" if st_dir == 1 else ("BEAR" if st_dir == -1 else "-")

        # VWAP relationship
        ltp = snap.get("close")
        vwap = snap.get("vwap")
        vwap_rel = "—"
        if ltp is not None and vwap is not None:
            diff_pct = ((ltp - vwap) / vwap) * 100
            vwap_rel = f"{'ABOVE' if ltp > vwap else 'BELOW'} VWAP ({diff_pct:+.2f}%)"

        # EMA alignment
        e9 = snap.get("ema9")
        e21 = snap.get("ema21")
        e50 = snap.get("ema50")
        if e9 and e21 and e50:
            if e9 > e21 > e50:
                ema_align = "BULLISH (9>21>50)"
            elif e9 < e21 < e50:
                ema_align = "BEARISH (9<21<50)"
            else:
                ema_align = "MIXED"
        else:
            ema_align = "—"

        # Print block
        print("─" * 78)
        print(f"{sym}")
        print("─" * 78)
        if day_c is not None:
            chg_sign = "+" if day_chg >= 0 else ""
            print(f"  Price        :  ₹{day_c:>10.2f}   "
                  f"(today: {chg_sign}₹{day_chg:.2f} / {chg_sign}{day_chg_pct:.2f}%)")
            print(f"  Day O/H/L    :  O ₹{day_o:.2f}   H ₹{day_h:.2f}   L ₹{day_l:.2f}")
        print(f"  RSI(14)      :  {fmt(snap.get('rsi'), 1):>10}    "
              f"{'(overbought)' if (snap.get('rsi') or 0) > 70 else '(oversold)' if (snap.get('rsi') or 100) < 30 else '(neutral)'}")
        print(f"  MACD         :  {fmt(snap.get('macd'), 3):>10}   "
              f"signal {fmt(snap.get('macd_signal'), 3)}   "
              f"hist {fmt(snap.get('macd_hist'), 3)}")
        print(f"  EMA 9/21/50  :  {fmt(e9)} / {fmt(e21)} / {fmt(e50)}   →  {ema_align}")
        print(f"  VWAP         :  {fmt(vwap):>10}    {vwap_rel}")
        print(f"  Bollinger    :  upper {fmt(snap.get('bb_upper'))}   "
              f"lower {fmt(snap.get('bb_lower'))}")
        print(f"  Supertrend   :  {fmt(snap.get('supertrend')):>10}    [{st_label}]")
        if snap.get("pattern"):
            print(f"  Pattern      :  {snap.get('pattern')}")
        print(f"  SIGNAL       :  {sig.type.value}   —   {sig.reason}")

        summary.append({"symbol": sym, "ltp": ltp, "vwap": vwap,
                        "rsi": snap.get("rsi"), "signal": sig.type.value,
                        "st_dir": st_dir, "change_pct": day_chg_pct})

    # Footer summary
    print("─" * 78)
    print("SUMMARY")
    print("─" * 78)
    n_above = sum(1 for r in summary if r["ltp"] and r["vwap"] and r["ltp"] > r["vwap"])
    n_below = sum(1 for r in summary if r["ltp"] and r["vwap"] and r["ltp"] < r["vwap"])
    n_bull_st = sum(1 for r in summary if r["st_dir"] == 1)
    n_bear_st = sum(1 for r in summary if r["st_dir"] == -1)
    n_buy = sum(1 for r in summary if r["signal"] == "BUY")
    n_exit = sum(1 for r in summary if r["signal"] == "EXIT")
    print(f"  Above VWAP    :  {n_above} / {len(summary)}")
    print(f"  Below VWAP    :  {n_below} / {len(summary)}")
    print(f"  Supertrend    :  {n_bull_st} bull  |  {n_bear_st} bear")
    print(f"  BUY signals   :  {n_buy}")
    print(f"  EXIT signals  :  {n_exit}")
    print("=" * 78)


if __name__ == "__main__":
    main()
