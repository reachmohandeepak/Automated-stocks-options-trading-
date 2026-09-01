"""
backtest_quick_profit.py
------------------------
Test the rule: invest ~Rs 3k per trade, exit when premium gain hits Rs 500.

Reuses the same delta-approximation model as backtest_options.py but with:
  * Fixed quick-profit exit: exit when (current_premium - entry_premium) * 20 >= 500
  * Same stops/squareoff/expiry as original
  * Compares "+Rs 500 quick exit" vs "0.8% spot target" head-to-head
"""

from __future__ import annotations
import sys, warnings
warnings.filterwarnings("ignore")
try: sys.stdout.reconfigure(encoding="utf-8")
except: pass

import pandas as pd
import indicators as ind
from datetime import datetime, time as dtime
from collections import Counter
from backtest_options import (
    UNDERLYINGS, OptionTrade, fetch_spot_history, score_setup,
    model_entry_premium, model_premium, report,
    TRADING_OPEN, TRADING_CLOSE, SQUAREOFF_TIME,
    CATASTROPHIC_STOP_PCT,
)


def simulate_quick_profit(under, df, profit_target_rs=500.0, stop_pct=0.004):
    """Same flow as backtest_options.simulate but exits early on Rs 500 profit."""
    df = ind.compute_all(df)
    trades = []
    in_position = False
    direction = ""
    entry_premium = entry_spot = stop_spot = 0.0
    entry_time = None

    for i in range(60, len(df) - 1):
        now = df.index[i].to_pydatetime()
        row = df.iloc[i]
        next_row = df.iloc[i + 1]
        history = df.iloc[max(0, i - 5):i + 1]

        if in_position:
            hours_held = (now - entry_time).total_seconds() / 3600.0
            cur_premium = model_premium(entry_premium, entry_spot,
                                         row["close"], direction, hours_held)

            exit_premium = None
            exit_reason = None

            # 0. NEW RULE: Quick profit at +Rs 500
            current_profit = (cur_premium - entry_premium) * under.lot_size
            if current_profit >= profit_target_rs:
                exit_premium = cur_premium
                exit_reason = f"QUICK_PROFIT_+{int(current_profit)}"

            # 1. Squareoff
            elif next_row.name.to_pydatetime().time() >= SQUAREOFF_TIME:
                exit_premium = model_premium(entry_premium, entry_spot,
                                              next_row["open"], direction, hours_held)
                exit_reason = "SQUAREOFF_315PM"

            # 2. Catastrophic premium stop
            elif cur_premium <= entry_premium * (1 - CATASTROPHIC_STOP_PCT):
                exit_premium = entry_premium * (1 - CATASTROPHIC_STOP_PCT)
                exit_reason = "CATASTROPHIC"

            # 3. Spot stop
            elif direction == "BUY_CALL" and row["low"] <= stop_spot:
                exit_premium = model_premium(entry_premium, entry_spot,
                                              stop_spot, direction, hours_held)
                exit_reason = "STOP_HIT"
            elif direction == "BUY_PUT" and row["high"] >= stop_spot:
                exit_premium = model_premium(entry_premium, entry_spot,
                                              stop_spot, direction, hours_held)
                exit_reason = "STOP_HIT"

            # 4. Signal flip
            if exit_reason is None:
                new_dir, new_score, _ = score_setup(row, history)
                if new_dir != "NO_TRADE" and new_dir != direction:
                    exit_premium = cur_premium
                    exit_reason = "SIGNAL_FLIPPED"

            if exit_reason:
                trades.append(OptionTrade(
                    underlying=under.name, direction=direction,
                    entry_time=entry_time,
                    exit_time=next_row.name.to_pydatetime(),
                    entry_spot=entry_spot, exit_spot=next_row["open"],
                    entry_premium=entry_premium, exit_premium=exit_premium,
                    lot_size=under.lot_size,
                    stop_spot=stop_spot, target_spot=0,
                    reason_entry="quick-profit-rule", reason_exit=exit_reason,
                ))
                in_position = False

        if (not in_position and now.time() >= TRADING_OPEN
                and now.time() < dtime(14, 55)):
            new_dir, score, reason = score_setup(row, history)
            if new_dir in ("BUY_CALL", "BUY_PUT") and score >= 4:
                entry_spot = float(next_row["open"])
                entry_premium = model_entry_premium(entry_spot)
                entry_time = next_row.name.to_pydatetime()
                direction = new_dir
                if direction == "BUY_CALL":
                    stop_spot = entry_spot * (1 - stop_pct)
                else:
                    stop_spot = entry_spot * (1 + stop_pct)
                in_position = True
    return trades


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--target", type=float, default=500.0,
                   help="Quick-profit target in Rs (default 500)")
    p.add_argument("--compare", action="store_true",
                   help="Sweep multiple targets: 200, 300, 400, 500, 750, 1000")
    args = p.parse_args()

    targets = [200, 300, 400, 500, 750, 1000] if args.compare else [args.target]

    df_cache = {}
    for u in UNDERLYINGS:
        if u.name != "SENSEX":
            continue
        print(f"[{u.name}]  Fetching {u.yf_symbol}...", end=" ", flush=True)
        df = fetch_spot_history(u.yf_symbol, 60)
        if df is None or len(df) < 100:
            print("  -> SKIPPED")
            return
        print(f"  -> {len(df)} bars")
        df_cache[u.name] = (u, df)

    sweep_results = []
    for target in targets:
        print()
        print("=" * 78)
        print(f"  Quick-profit target: Rs {target:.0f}  "
              f"(~{target/(0.005*76000*20)*100:.1f}% premium gain on ATM SENSEX)")
        print("=" * 78)
        all_quick = []
        for name, (u, df) in df_cache.items():
            trades = simulate_quick_profit(u, df, profit_target_rs=target)
            if not args.compare:
                report(trades, label=f"{u.name} (target Rs {target:.0f})")
            all_quick.extend(trades)

        if all_quick:
            wins = [t for t in all_quick if t.net_pnl > 0]
            losses = [t for t in all_quick if t.net_pnl <= 0]
            wr = len(wins) / len(all_quick) * 100
            total = sum(t.net_pnl for t in all_quick)
            avg_w = sum(t.net_pnl for t in wins) / len(wins) if wins else 0
            avg_l = sum(t.net_pnl for t in losses) / len(losses) if losses else 0
            pf = (sum(t.net_pnl for t in wins) / abs(sum(t.net_pnl for t in losses))
                  if losses else float("inf"))
            # Realistic friction: ~Rs 50 brokerage/trade + ~0.4% slippage on premium
            real_fees = len(all_quick) * 50 + sum(
                abs(t.entry_premium) * 20 * 0.004 for t in all_quick)
            real_net = total - real_fees
            sweep_results.append({
                "target": target, "trades": len(all_quick),
                "win_rate": wr, "total": total, "real_net": real_net,
                "avg_w": avg_w, "avg_l": avg_l, "pf": pf, "real_fees": real_fees,
            })
            print(f"  Trades       : {len(all_quick)}    Win rate: {wr:.1f}%")
            print(f"  Avg win/loss : Rs {avg_w:+.0f} / Rs {avg_l:+.0f}")
            print(f"  Gross P&L    : Rs {total:+,.0f}")
            print(f"  Real fees    : Rs {real_fees:,.0f}  "
                  f"(Rs 50 + 0.4% slippage per trade)")
            print(f"  NET P&L      : Rs {real_net:+,.0f}    PF: "
                  f"{'inf' if pf == float('inf') else f'{pf:.2f}'}")

    if args.compare and len(sweep_results) > 1:
        print()
        print("=" * 78)
        print("  TARGET SWEEP — ranked by REAL NET P&L (after friction)")
        print("=" * 78)
        print(f"  {'Target':>8} {'Trades':>7} {'WinRate':>8} {'AvgWin':>8} "
              f"{'AvgLoss':>9} {'Gross':>10} {'Fees':>9} {'NET':>10}  PF")
        for r in sorted(sweep_results, key=lambda x: -x["real_net"]):
            pf_s = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
            print(f"  Rs {r['target']:>5.0f} {r['trades']:>7} "
                  f"{r['win_rate']:>6.1f}% Rs {r['avg_w']:>+5.0f} "
                  f"Rs {r['avg_l']:>+6.0f} Rs {r['total']:>+7.0f} "
                  f"Rs {r['real_fees']:>5.0f} Rs {r['real_net']:>+7.0f}  {pf_s}")
        print()
        best = max(sweep_results, key=lambda x: x["real_net"])
        print(f"  >>> Best target by real net P&L: Rs {best['target']:.0f} "
              f"(net Rs {best['real_net']:+,.0f} over 60 days)")
        return  # skip the default conclusion block

    all_quick = []
    for name, (u, df) in df_cache.items():
        all_quick.extend(simulate_quick_profit(u, df, profit_target_rs=targets[0]))

    print()
    print("=" * 78)
    print("  CONCLUSION")
    print("=" * 78)
    if all_quick:
        wins = [t for t in all_quick if t.net_pnl > 0]
        win_rate = len(wins) / len(all_quick) * 100
        total = sum(t.net_pnl for t in all_quick)
        avg_w = sum(t.net_pnl for t in wins) / len(wins) if wins else 0
        losses = [t for t in all_quick if t.net_pnl <= 0]
        avg_l = sum(t.net_pnl for t in losses) / len(losses) if losses else 0
        print(f"  Win rate: {win_rate:.1f}%  (need 55-70% to be profitable)")
        print(f"  Avg win:  Rs {avg_w:+.0f}   Avg loss: Rs {avg_l:+.0f}")
        print(f"  Net P&L:  Rs {total:+,.0f} over 60 days")
        if win_rate >= 60 and total > 0:
            print(f"  -> Rule WORKS — high enough win rate to overcome larger losses")
        elif win_rate >= 50 and total > 0:
            print(f"  -> Rule MARGINAL — profitable but skinnier than expected")
        else:
            print(f"  -> Rule does NOT improve outcomes — asymmetric losses dominate")


if __name__ == "__main__":
    main()
