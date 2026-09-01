"""
analyze_audit.py
----------------
End-of-period audit of the options bot's signals-only run.

Reads logs/options_orders.csv, pairs ENTRY+EXIT events, and reports:
  * Total trades, wins/losses, win rate
  * Modeled net P&L (uses logged premiums; subtracts brokerage estimate)
  * Profit factor
  * Average win / average loss
  * Hold-time distribution
  * Exit reason breakdown
  * Per-day P&L summary

Run:
    python analyze_audit.py                   # all trades
    python analyze_audit.py --dry-run-only    # exclude live trades
    python analyze_audit.py --since 2026-05-26
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

LOG_PATH = "logs/options_orders.csv"

# Per-round-trip brokerage estimate (Zerodha intraday options)
BROKERAGE_PER_ROUND_TRIP = 50.0


def parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def load_events(path: str, since: Optional[datetime] = None,
                 dry_run_only: bool = False) -> list:
    if not os.path.exists(path):
        print(f"Log file not found: {path}")
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts = parse_dt(r.get("timestamp_ist", ""))
            if since and ts and ts < since:
                continue
            if dry_run_only and not str(r.get("order_id", "")).startswith("DRY"):
                continue
            r["_ts"] = ts
            rows.append(r)
    return rows


def pair_trades(events: list) -> list:
    """Walk events in time order and pair each ENTRY with its next EXIT
    on the same contract."""
    by_contract: dict = defaultdict(list)
    for e in events:
        by_contract[e["contract"]].append(e)
    trades = []
    for contract, evs in by_contract.items():
        evs.sort(key=lambda x: x["_ts"] or datetime.min)
        open_entry = None
        for e in evs:
            if e["event"] == "ENTRY":
                open_entry = e
            elif e["event"] == "EXIT" and open_entry is not None:
                trades.append({
                    "contract": contract,
                    "underlying": e.get("underlying", ""),
                    "entry_time": open_entry["_ts"],
                    "exit_time": e["_ts"],
                    "entry_premium": float(open_entry.get("premium") or 0),
                    "exit_premium": float(e.get("premium") or 0),
                    "qty_units": int(float(open_entry.get("qty_units") or 0)),
                    "gross_pnl_logged": float(e.get("pnl") or 0),
                    "reason_entry": open_entry.get("reason", ""),
                    "reason_exit": e.get("reason", ""),
                    "order_id_entry": open_entry.get("order_id", ""),
                    "order_id_exit": e.get("order_id", ""),
                    "is_dry_run": str(open_entry.get("order_id", "")).startswith("DRY"),
                })
                open_entry = None
    trades.sort(key=lambda t: t["entry_time"] or datetime.min)
    return trades


def report(trades: list, label: str = "AUDIT REPORT"):
    if not trades:
        print(f"\n{label}: no completed trades found")
        return

    n = len(trades)
    wins = [t for t in trades if t["gross_pnl_logged"] > 0]
    losses = [t for t in trades if t["gross_pnl_logged"] <= 0]
    gross = sum(t["gross_pnl_logged"] for t in trades)
    # Brokerage applies only to LIVE trades; dry-run = no fees
    fees = sum(BROKERAGE_PER_ROUND_TRIP for t in trades if not t["is_dry_run"])
    net = gross - fees
    avg_win = sum(t["gross_pnl_logged"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["gross_pnl_logged"] for t in losses) / len(losses) if losses else 0
    pf = (sum(t["gross_pnl_logged"] for t in wins) /
          abs(sum(t["gross_pnl_logged"] for t in losses))) if losses else float("inf")

    print()
    print("=" * 78)
    print(f"  {label}")
    print("=" * 78)
    print(f"  Period: {trades[0]['entry_time']} to {trades[-1]['exit_time']}")
    print(f"  Total trades   : {n}   (Wins {len(wins)} / Losses {len(losses)})")
    print(f"  Win rate       : {len(wins)/n*100:.1f}%")
    print(f"  Gross P&L      : Rs {gross:+,.2f}    (sum of logged trade P&Ls)")
    print(f"  Brokerage est  : Rs {fees:.2f}    "
          f"(Rs {BROKERAGE_PER_ROUND_TRIP:.0f} × {len([t for t in trades if not t['is_dry_run']])} live trades)")
    print(f"  NET P&L        : Rs {net:+,.2f}")
    print(f"  Avg win        : Rs {avg_win:+,.2f}    Avg loss: Rs {avg_loss:+,.2f}")
    pf_str = f"{pf:.2f}" if pf != float("inf") else "infinite"
    print(f"  Profit factor  : {pf_str}    (>1.5 good, >2.0 great)")

    # Hold-time distribution
    holds = []
    for t in trades:
        if t["entry_time"] and t["exit_time"]:
            holds.append((t["exit_time"] - t["entry_time"]).total_seconds() / 60)
    if holds:
        avg_hold = sum(holds) / len(holds)
        min_hold = min(holds)
        max_hold = max(holds)
        instant = sum(1 for h in holds if h < 1)
        print(f"  Avg hold       : {avg_hold:.1f} min   (min {min_hold:.1f}, max {max_hold:.1f})")
        if instant > 0:
            print(f"  ⚠️  Instant exits (<1min): {instant} — likely bot bug!")

    # Exit reasons
    reasons = Counter(t["reason_exit"] for t in trades)
    print(f"  Exits          : {dict(reasons)}")

    # Best/worst
    best = max(trades, key=lambda t: t["gross_pnl_logged"])
    worst = min(trades, key=lambda t: t["gross_pnl_logged"])
    print(f"  Best trade     : {best['contract']} {best['entry_time'].strftime('%m-%d %H:%M')} "
          f"→ Rs {best['gross_pnl_logged']:+,.2f} ({best['reason_exit']})")
    print(f"  Worst trade    : {worst['contract']} {worst['entry_time'].strftime('%m-%d %H:%M')} "
          f"→ Rs {worst['gross_pnl_logged']:+,.2f} ({worst['reason_exit']})")

    # Per-day summary
    by_day = defaultdict(list)
    for t in trades:
        if t["entry_time"]:
            by_day[t["entry_time"].date()].append(t)
    print()
    print("  Per-day P&L:")
    print(f"  {'Date':<12} {'Trades':>7} {'Wins':>5} {'Gross':>10} {'Fees':>8} {'Net':>10}")
    cumulative = 0
    for day in sorted(by_day):
        day_trades = by_day[day]
        day_gross = sum(t["gross_pnl_logged"] for t in day_trades)
        day_wins = sum(1 for t in day_trades if t["gross_pnl_logged"] > 0)
        day_fees = sum(BROKERAGE_PER_ROUND_TRIP for t in day_trades if not t["is_dry_run"])
        day_net = day_gross - day_fees
        cumulative += day_net
        marker = "✅" if day_net > 0 else "❌" if day_net < 0 else "  "
        print(f"  {str(day):<12} {len(day_trades):>7} {day_wins:>5} "
              f"Rs {day_gross:>+7.0f} Rs {day_fees:>5.0f} Rs {day_net:>+7.0f}  {marker}")
    print(f"  {'CUMULATIVE':<12} {' '*23} {' '*8} Rs {cumulative:>+7.0f}")

    # Verdict
    print()
    print("=" * 78)
    print("  VERDICT")
    print("=" * 78)
    if n < 10:
        print(f"  ⚠️  Only {n} trades — too few for statistical confidence.")
        print(f"     Continue running signals-only for more data.")
    elif pf >= 1.5 and net > 0 and len(wins)/n >= 0.45:
        print(f"  ✅ Strategy has positive edge (PF {pf:.2f}, net Rs {net:+,.0f}).")
        print(f"     Could consider live trading. Start small.")
    elif pf >= 1.0 and net > -500:
        print(f"  ⚠️  Strategy is borderline (PF {pf:.2f}, net Rs {net:+,.0f}).")
        print(f"     Gather more data OR refine before live trading.")
    else:
        print(f"  ❌ Strategy is NOT profitable (PF {pf:.2f}, net Rs {net:+,.0f}).")
        print(f"     Do NOT go live. Strategy needs significant rework.")
    print()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run-only", action="store_true",
                   help="Include only dry-run trades (order_id starting with DRY)")
    p.add_argument("--since", type=str, default=None,
                   help="Only trades since this date (YYYY-MM-DD)")
    p.add_argument("--log", type=str, default=LOG_PATH)
    args = p.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
            # CSV timestamps are tz-aware (IST). Make `since` tz-aware too.
            if since.tzinfo is None:
                import pytz
                since = pytz.timezone("Asia/Kolkata").localize(since)
        except Exception as e:
            print(f"Bad date: {args.since} ({e})")
            return

    events = load_events(args.log, since=since, dry_run_only=args.dry_run_only)
    if not events:
        print("No events in log.")
        return

    trades = pair_trades(events)
    label = "AUDIT REPORT"
    if args.dry_run_only:
        label += " (dry-run only)"
    if args.since:
        label += f" (since {args.since})"
    report(trades, label=label)


if __name__ == "__main__":
    main()
