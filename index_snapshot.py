"""
index_snapshot.py
-----------------
Snapshot of major Indian market indices via yfinance — Nifty, Sensex,
Bank Nifty, sector indices, India VIX, etc.

Indices aren't directly tradeable (no cash market), but they're the
single best gauge of overall market direction. If Nifty is deeply red
on the day, even technically-perfect BUY setups on individual stocks
tend to fail — so this is a useful sanity-check before any entry.

Run anytime:   python index_snapshot.py
"""

import sys
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import yfinance as yf


# yfinance symbols for major Indian indices
INDICES = {
    "NIFTY 50":          "^NSEI",
    "SENSEX":            "^BSESN",
    "BANK NIFTY":        "^NSEBANK",
    "NIFTY IT":          "^CNXIT",
    "NIFTY AUTO":        "^CNXAUTO",
    "NIFTY FMCG":        "^CNXFMCG",
    "NIFTY PHARMA":      "^CNXPHARMA",
    "NIFTY METAL":       "^CNXMETAL",
    "NIFTY MIDCAP 100":  "^NSEMDCP50",
    "INDIA VIX":         "^INDIAVIX",
}


def fmt_change(change, pct):
    """Render +/- with arrow + color hint via plain ASCII."""
    if change is None or pct is None:
        return "—"
    sign = "+" if change >= 0 else ""
    arrow = "^" if change >= 0 else "v"
    return f"{arrow} {sign}{change:>8.2f}  ({sign}{pct:.2f}%)"


def main():
    print()
    print("=" * 78)
    print(f"  INDIAN INDICES SNAPSHOT  —  yfinance  —  "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)
    print(f"  {'Index':<20} {'Last':>10} {'Day High':>10} {'Day Low':>10} "
          f"{'Change':>22}")
    print("-" * 78)

    bull = bear = 0
    for name, sym in INDICES.items():
        try:
            df = yf.Ticker(sym).history(period="2d", interval="5m", auto_adjust=False)
        except Exception as e:
            print(f"  {name:<20} {'(fetch error: ' + str(e)[:40] + ')'}")
            continue
        if df is None or df.empty:
            print(f"  {name:<20} {'(no data)':<20}")
            continue

        # Today's session
        today = df.index[-1].date()
        today_df = df[df.index.date == today]
        if today_df.empty:
            # Maybe no intraday yet — use the latest available row
            last = df.iloc[-1]
            print(f"  {name:<20} {last['Close']:>10.2f} {'—':>10} {'—':>10} "
                  f"{'(off-session)':>22}")
            continue

        last_close = today_df["Close"].iloc[-1]
        day_open = today_df["Open"].iloc[0]
        day_high = today_df["High"].max()
        day_low = today_df["Low"].min()
        change = last_close - day_open
        pct = (change / day_open) * 100 if day_open else 0

        if change >= 0:
            bull += 1
        else:
            bear += 1

        print(f"  {name:<20} {last_close:>10.2f} {day_high:>10.2f} "
              f"{day_low:>10.2f} {fmt_change(change, pct):>22}")

    print("-" * 78)
    total = bull + bear
    if total:
        mood = ("RISK-ON (broad strength)" if bull > bear * 2
                else "RISK-OFF (broad weakness)" if bear > bull * 2
                else "MIXED")
        print(f"  Breadth: {bull} up  |  {bear} down  →  {mood}")

    print("=" * 78)
    print("  Note: data is ~15 min delayed during market hours.")
    print("  Indices are not directly tradeable — use these as direction filters.")
    print("=" * 78)


if __name__ == "__main__":
    main()
