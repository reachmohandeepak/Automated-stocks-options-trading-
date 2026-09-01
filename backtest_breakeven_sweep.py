"""
backtest_breakeven_sweep.py
---------------------------
Tests whether a BREAKEVEN STOP helps or hurts, using the live-bot exit model
(rupee quick-loss + quick-profit ratchet). Answers the question:

  "Once a trade is modestly green, should we move the stop to breakeven so a
   would-be winner can't become a loser?"

The risk: the edge is a few big ratchet winners, and a breakeven stop may scratch
trades that dipped then would have recovered into those winners. This measures it.

Every config uses the SAME entries + ratchet; only the breakeven arm changes.

Run:
    python backtest_breakeven_sweep.py            # SENSEX
    python backtest_breakeven_sweep.py --all      # all 3 indices
"""

from __future__ import annotations
import argparse
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from backtest_options import UNDERLYINGS, fetch_spot_history, simulate

# Live-bot exit model, defaults matching production (per-lot rupee).
BASE = dict(
    stop_pct=0.004, target_pct=0.008, skip_first_min=0, min_adx=0.0,
    strict_threshold=False, use_rupee_exits=True,
    quick_loss_rs=150.0, quick_profit_rs=300.0, ratchet_step_rs=50.0,
)

# (label, breakeven_arm_rs)  — 0 = breakeven stop OFF (baseline)
GRID = [
    ("OFF (baseline)", 0.0),
    ("breakeven@75",   75.0),
    ("breakeven@100",  100.0),
    ("breakeven@150",  150.0),
    ("breakeven@200",  200.0),
    ("breakeven@250",  250.0),
]


def stats(trades):
    if not trades:
        return dict(n=0, w=0, wr=0.0, total=0.0, pf=0.0, be=0)
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    wpnl, lpnl = sum(wins), sum(losses)
    be = sum(1 for t in trades if t.reason_exit == "BREAKEVEN_STOP")
    return dict(
        n=len(trades), w=len(wins), wr=len(wins) / len(trades) * 100,
        total=wpnl + lpnl, pf=(wpnl / abs(lpnl) if lpnl else float("inf")), be=be,
    )


def main():
    p = argparse.ArgumentParser(description="Breakeven-stop sweep (live exit model).")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--all", action="store_true", help="All 3 indices (default SENSEX)")
    args = p.parse_args()

    actives = UNDERLYINGS if args.all else [u for u in UNDERLYINGS if u.name == "SENSEX"]

    print()
    print("=" * 90)
    print(f"  BREAKEVEN-STOP SWEEP  —  {args.days}-day  —  "
          f"{'ALL indices' if args.all else 'SENSEX'}  —  live exit model "
          f"(quick-loss 150 / ratchet 300+50)")
    print("=" * 90)

    cache = []
    for u in actives:
        print(f"  [{u.name}] fetching...", end=" ", flush=True)
        df = fetch_spot_history(u.yf_symbol, args.days)
        if df is None or len(df) < 100:
            print("SKIPPED"); continue
        print(f"{len(df)} bars")
        cache.append((u, df))
    if not cache:
        print("No data — aborting."); return

    results = []
    for label, be in GRID:
        all_t = []
        for u, df in cache:
            all_t.extend(simulate(u, df, breakeven_arm_rs=be, **BASE))
        s = stats(all_t); s["label"] = label
        results.append(s)

    print()
    print(f"  {'config':<18}{'trades':>7}{'win%':>7}{'BE-exits':>9}{'net P&L':>12}{'PF':>7}")
    print("-" * 90)
    for r in results:
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"  {r['label']:<18}{r['n']:>7}{r['wr']:>6.0f}%{r['be']:>9}"
              f"{r['total']:>+12,.0f}{pf:>7}")

    base = results[0]["total"]
    print()
    print("  vs baseline (breakeven OFF):")
    for r in results[1:]:
        d = r["total"] - base
        verdict = "HELPS" if d > 0 else "HURTS"
        print(f"    {r['label']:<18} {d:>+8,.0f}  ({verdict})")
    print()
    print("  CAVEAT: delta-approx premiums, bar-close evaluation (no intrabar). "
          "Directional, not exact.")
    print()


if __name__ == "__main__":
    main()
