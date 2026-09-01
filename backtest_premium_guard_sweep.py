"""
backtest_premium_guard_sweep.py
-------------------------------
Sweeps the premium-chase guard ("Limit 30%") through the options backtester to
answer: "Does refusing entries when the option has already run up X% above its
day-open premium help or hurt?"

The live bot blocks entry when premium > 30% above today's open (SAFETY 3 in
options_bot.py). This sweep re-prices each candidate contract at the day-open
spot via the delta model and applies the same percentage gate, holding every
other parameter at baseline so the ONLY thing changing is the limit.

Limit values are fractions: 0.30 = block if premium ran up >30% since open.
"off" disables the guard entirely (take every signal regardless of run-up).

Run:
    python backtest_premium_guard_sweep.py            # SENSEX (small-cap default)
    python backtest_premium_guard_sweep.py --all      # all 3 indices aggregated
    python backtest_premium_guard_sweep.py --days 30
"""

from __future__ import annotations

import argparse
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from backtest_options import UNDERLYINGS, fetch_spot_history, simulate

# Baseline — identical to SWEEP_CONFIGS["baseline"], no cooldown.
BASE = dict(stop_pct=0.004, target_pct=0.008, skip_first_min=0,
            min_adx=0.0, strict_threshold=False,
            use_rupee_exits=True, quick_loss_rs=150.0,
            quick_profit_rs=300.0, ratchet_step_rs=50.0)

# (label, max_premium_above_open)  — 0.0 means guard OFF.
GUARD_GRID = [
    ("off (take every signal)", 0.0),
    ("limit 15%",               0.15),
    ("limit 20%",               0.20),
    ("limit 30% (current)",     0.30),
    ("limit 40%",               0.40),
    ("limit 50%",               0.50),
    ("limit 75%",               0.75),
    ("limit 100%",              1.00),
]


def stats(trades):
    if not trades:
        return dict(trades=0, win_rate=0.0, total=0.0, pf=0.0, avg_w=0.0, avg_l=0.0)
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    total = sum(t.net_pnl for t in trades)
    wpnl, lpnl = sum(wins), sum(losses)
    pf = wpnl / abs(lpnl) if lpnl else float("inf")
    return dict(
        trades=len(trades),
        win_rate=len(wins) / len(trades) * 100,
        total=total,
        pf=pf,
        avg_w=(wpnl / len(wins)) if wins else 0.0,
        avg_l=(lpnl / len(losses)) if losses else 0.0,
    )


def main():
    p = argparse.ArgumentParser(description="Premium-chase guard sweep.")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--all", action="store_true",
                   help="Aggregate all 3 indices (default: SENSEX only)")
    args = p.parse_args()

    actives = UNDERLYINGS if args.all else [u for u in UNDERLYINGS if u.name == "SENSEX"]

    print()
    print("=" * 92)
    print(f"  PREMIUM-CHASE GUARD SWEEP  —  {args.days}-day 5-min spot  —  "
          f"{'ALL indices' if args.all else 'SENSEX only'}")
    print("=" * 92)
    print("  Baseline: stop -0.4% / target +0.8% (2:1), no ADX, no cooldown.")
    print("  Only the premium-above-open limit changes between rows.")

    cache = []
    for u in actives:
        print(f"\n  [{u.name}] fetching {u.yf_symbol}...", end=" ", flush=True)
        df = fetch_spot_history(u.yf_symbol, args.days)
        if df is None or len(df) < 100:
            print("→ SKIPPED (no data)")
            continue
        print(f"→ {len(df)} bars ({df.index[0].date()} to {df.index[-1].date()})")
        cache.append((u, df))

    if not cache:
        print("\n  No data — aborting (yfinance may be rate-limited; retry).")
        return

    results = []
    for label, limit in GUARD_GRID:
        all_trades = []
        for u, df in cache:
            all_trades.extend(simulate(
                u, df, max_premium_above_open=limit, **BASE))
        s = stats(all_trades)
        s["label"] = label
        results.append(s)

    # Trades taken under "off" = total candidate signals; use to show how many
    # each limit filters out.
    base_trades = results[0]["trades"] or 1

    print()
    print("=" * 92)
    print(f"  {'guard config':<26}{'trades':>7}{'filtered':>9}{'win%':>8}"
          f"{'net P&L':>12}{'PF':>7}{'avg win':>10}{'avg loss':>10}")
    print("-" * 92)
    for r in results:
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        filtered = base_trades - r["trades"]
        print(f"  {r['label']:<26}{r['trades']:>7}{filtered:>9}{r['win_rate']:>7.1f}%"
              f"{r['total']:>+12,.0f}{pf:>7}{r['avg_w']:>+10,.0f}{r['avg_l']:>+10,.0f}")

    def pf_key(r):
        return r["pf"] if r["pf"] != float("inf") else 1e9

    print()
    print("=" * 92)
    print("  RANKED BY TOTAL NET P&L")
    print("-" * 92)
    for i, r in enumerate(sorted(results, key=lambda r: r["total"], reverse=True), 1):
        print(f"   {i:>2}. {r['label']:<26} Rs {r['total']:>+9,.0f}   "
              f"(trades={r['trades']}, win={r['win_rate']:.1f}%)")

    print()
    print("  RANKED BY PROFIT FACTOR")
    print("-" * 92)
    for i, r in enumerate(sorted(results, key=pf_key, reverse=True), 1):
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"   {i:>2}. {r['label']:<26} PF={pf:<6}  "
              f"(P&L=Rs {r['total']:+,.0f}, trades={r['trades']})")

    print()
    print("  CAVEAT: the guard is modelled by re-pricing the entry contract at the")
    print("          day-open spot with delta=0.5. Real options re-price non-linearly")
    print("          (gamma/IV), so treat these as directional, not exact.")
    print()


if __name__ == "__main__":
    main()
