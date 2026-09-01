"""
backtest_improve_sweep.py
-------------------------
Hunts for improvements using the LIVE exit model (rupee quick-loss + ratchet)
across all indices. Sweeps one lever at a time so each result is attributable:

  1. Opening filter  (skip first N min)  — targets the morning-whipsaw losses
  2. ADX floor       (trend-strength gate) — targets chop losses
  3. Post-loss cooldown                    — targets over-trading the dead move

Everything else held at production defaults. 60-day, 5-min spot, delta-approx.

Run:  python backtest_improve_sweep.py   (add --days 30 for a shorter window)
"""
from __future__ import annotations
import argparse, sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass
from datetime import time as dtime
from backtest_options import UNDERLYINGS, fetch_spot_history, simulate

BASE = dict(stop_pct=0.004, target_pct=0.008, use_rupee_exits=True,
            quick_loss_rs=150.0, quick_profit_rs=300.0, ratchet_step_rs=50.0)


def stats(trades):
    if not trades:
        return (0, 0.0, 0.0, 0.0)
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    wpnl, lpnl = sum(wins), sum(losses)
    pf = wpnl / abs(lpnl) if lpnl else float("inf")
    return (len(trades), len(wins) / len(trades) * 100, wpnl + lpnl, pf)


def run(cache, **over):
    all_t = []
    for u, df in cache:
        all_t.extend(simulate(u, df, **{**BASE, **over}))
    return stats(all_t)


def section(title, cache, rows):
    print("\n" + "=" * 78 + f"\n  {title}\n" + "-" * 78)
    print(f"  {'config':<22}{'trades':>7}{'win%':>7}{'net P&L':>12}{'PF':>7}")
    best = None
    for label, over in rows:
        n, wr, tot, pf = run(cache, **over)
        pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"  {label:<22}{n:>7}{wr:>6.0f}%{tot:>+12,.0f}{pfs:>7}")
        if best is None or tot > best[1]:
            best = (label, tot)
    print(f"  -> best by P&L: {best[0]} (Rs {best[1]:+,.0f})")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=60)
    args = p.parse_args()

    print("\n" + "#" * 78)
    print(f"  IMPROVEMENT SWEEP — {args.days}-day, all indices, LIVE exit model")
    print("#" * 78)
    cache = []
    for u in UNDERLYINGS:
        df = fetch_spot_history(u.yf_symbol, args.days)
        if df is not None and len(df) >= 100:
            cache.append((u, df))
            print(f"  [{u.name}] {len(df)} bars")
    if not cache:
        print("No data."); return

    section("1) OPENING FILTER (skip first N minutes)", cache, [
        ("none (0 min)",     dict(skip_first_min=0)),
        ("skip 15 min",      dict(skip_first_min=15)),
        ("skip 30 min",      dict(skip_first_min=30)),
        ("skip 45 min",      dict(skip_first_min=45)),
        ("skip 60 min",      dict(skip_first_min=60)),
    ])
    section("2) ADX FLOOR (trend-strength gate)", cache, [
        ("ADX off (0)",  dict(min_adx=0.0)),
        ("ADX >= 20",    dict(min_adx=20.0)),
        ("ADX >= 25",    dict(min_adx=25.0)),
        ("ADX >= 30",    dict(min_adx=30.0)),
        ("ADX >= 35",    dict(min_adx=35.0)),
    ])
    section("3) POST-LOSS COOLDOWN", cache, [
        ("none",         dict(cooldown_sec=0, cooldown_loss_sec=0)),
        ("loss 5 min",   dict(cooldown_sec=0, cooldown_loss_sec=300)),
        ("loss 10 min",  dict(cooldown_sec=0, cooldown_loss_sec=600)),
        ("loss 15 min",  dict(cooldown_sec=0, cooldown_loss_sec=900)),
        ("loss 30 min",  dict(cooldown_sec=0, cooldown_loss_sec=1800)),
    ])
    section("4) COMBINED (skip30 + ADX25 + loss-cooldown 10m)", cache, [
        ("baseline (none)",  dict()),
        ("combined",         dict(skip_first_min=30, min_adx=25.0,
                                  cooldown_sec=0, cooldown_loss_sec=600)),
    ])
    print("\n  CAVEAT: delta-approx premiums, bar-close exits. Directional, not exact ₹.\n")


if __name__ == "__main__":
    main()
