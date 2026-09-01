"""
backtest_cooldown_sweep.py
--------------------------
Sweeps post-exit cooldown values through the options backtester to answer:
"What cooldown period actually performs best on historical data?"

Holds every other parameter at baseline (stop 0.4% / target 0.8%, no ADX
filter) so the ONLY thing changing between rows is the cooldown. That isolates
cooldown's effect on P&L, win rate, profit factor, and trade count.

Cooldown values are in SECONDS, applied per-underlying. Post-loss lockout =
max(win-cooldown, loss-cooldown), mirroring the live bot's can_open().

Run:
    python backtest_cooldown_sweep.py                 # SENSEX (small-cap default)
    python backtest_cooldown_sweep.py --all           # all 3 indices aggregated
    python backtest_cooldown_sweep.py --days 30
"""

from __future__ import annotations

import argparse
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from backtest_options import (
    UNDERLYINGS, fetch_spot_history, simulate,
)

# Baseline config — identical to SWEEP_CONFIGS["baseline"] minus cooldown.
BASE = dict(stop_pct=0.004, target_pct=0.008, skip_first_min=0,
            min_adx=0.0, strict_threshold=False)

# (label, cooldown_sec_after_win, cooldown_sec_after_loss)
COOLDOWN_GRID = [
    ("no-cooldown (baseline)",   0,    0),
    ("symmetric 5m",             300,  300),
    ("symmetric 10m",            600,  600),
    ("symmetric 15m",            900,  900),
    ("symmetric 20m",            1200, 1200),
    ("symmetric 30m (current)",  1800, 1800),
    ("win 10m / loss 15m",       600,  900),
    ("win 10m / loss 20m",       600,  1200),
    ("win 5m / loss 15m",        300,  900),
    ("win 10m / loss 30m",       600,  1800),
]


def stats(trades):
    if not trades:
        return dict(trades=0, win_rate=0.0, total=0.0, pf=0.0,
                    avg_w=0.0, avg_l=0.0)
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
    p = argparse.ArgumentParser(description="Cooldown sweep for the options strategy.")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--all", action="store_true",
                   help="Aggregate all 3 indices (default: SENSEX only — the small-cap underlying)")
    args = p.parse_args()

    actives = UNDERLYINGS if args.all else [u for u in UNDERLYINGS if u.name == "SENSEX"]

    print()
    print("=" * 92)
    print(f"  COOLDOWN SWEEP  —  {args.days}-day 5-min spot  —  "
          f"{'ALL indices' if args.all else 'SENSEX only'}")
    print("=" * 92)
    print("  Baseline: stop -0.4% / target +0.8% (2:1), no ADX filter, "
          "entries until 14:55.")
    print("  Only the cooldown changes between rows. Post-loss lockout = "
          "max(win, loss).")

    # Fetch once, reuse for every cooldown config.
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
    for label, cd_win, cd_loss in COOLDOWN_GRID:
        all_trades = []
        for u, df in cache:
            all_trades.extend(simulate(
                u, df, cooldown_sec=cd_win, cooldown_loss_sec=cd_loss, **BASE))
        s = stats(all_trades)
        s["label"] = label
        results.append(s)

    # ----- Results table -----
    print()
    print("=" * 92)
    print(f"  {'cooldown config':<26}{'trades':>7}{'win%':>8}{'net P&L':>12}"
          f"{'PF':>7}{'avg win':>10}{'avg loss':>10}")
    print("-" * 92)
    for r in results:
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"  {r['label']:<26}{r['trades']:>7}{r['win_rate']:>7.1f}%"
              f"{r['total']:>+12,.0f}{pf:>7}{r['avg_w']:>+10,.0f}{r['avg_l']:>+10,.0f}")

    # ----- Rankings -----
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
    print("  CAVEAT: delta-approximation premiums (no IV/spread). Use this to "
          "rank cooldowns\n          relative to each other, not to predict exact rupees.")
    print()


if __name__ == "__main__":
    main()
