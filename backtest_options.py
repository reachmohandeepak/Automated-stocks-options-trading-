"""
backtest_options.py
-------------------
Walk-forward backtest of the OPTIONS direction strategy (BUY CALL / BUY PUT)
on 60 days of historical spot data for NIFTY, BANKNIFTY, SENSEX.

IMPORTANT CAVEAT — APPROXIMATION:
  Real historical Indian option premium data is not freely accessible.
  This backtest uses a delta-approximation model:
    * Entry premium  ≈ 0.5% of spot  (typical ATM weekly premium)
    * Premium move   ≈ (spot move) × 0.5   (ATM delta)
    * Intraday theta ≈ tiny (~0.05% premium/hour for short holds)
  This is DIRECTIONALLY ACCURATE — tells you if the signal has positive
  expected value — but actual ₹ P&L in live trading will vary 20-30% due
  to IV changes, theta specifics, and bid-ask spreads.

  Use this to answer: "Does the BUY logic have an edge?"
  Don't use it to predict exact ₹ profits.

Run:
    python backtest_options.py
    python backtest_options.py --days 30
    python backtest_options.py --underlying NIFTY
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import List, Optional, Tuple

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

import indicators as ind

IST = pytz.timezone("Asia/Kolkata")

TRADING_OPEN = dtime(9, 15)
TRADING_CLOSE = dtime(15, 30)
SQUAREOFF_TIME = dtime(15, 15)
EXPIRY_DAY_EXIT = dtime(14, 30)

# Model parameters (calibrated to typical Indian index options)
ATM_PREMIUM_PCT_OF_SPOT = 0.005    # ATM weekly ~ 0.5% of spot
DELTA_ATM = 0.5                    # ATM call delta ~ 0.5
THETA_PER_HOUR_PCT = 0.0005        # premium loses ~0.05%/hour intraday
CATASTROPHIC_STOP_PCT = 0.60       # exit if premium drops 60% (same as live bot)
COST_PCT_ROUNDTRIP = 0.006         # brokerage + slippage on options ~0.6% round-trip


# ----- Underlying configs (matches options_signal.py) ---------------------
@dataclass
class Underlying:
    name: str
    yf_symbol: str
    lot_size: int


UNDERLYINGS = [
    Underlying("NIFTY",     "^NSEI",     75),
    Underlying("BANKNIFTY", "^NSEBANK",  30),
    Underlying("SENSEX",    "^BSESN",    20),
]


# ----- Trade record --------------------------------------------------------
@dataclass
class OptionTrade:
    underlying: str
    direction: str          # BUY_CALL / BUY_PUT
    entry_time: datetime
    exit_time: datetime
    entry_spot: float
    exit_spot: float
    entry_premium: float    # modelled
    exit_premium: float     # modelled
    lot_size: int
    qty_lots: int = 1
    stop_spot: float = 0.0
    target_spot: float = 0.0
    reason_exit: str = ""
    reason_entry: str = ""

    @property
    def gross_pnl(self) -> float:
        return (self.exit_premium - self.entry_premium) * self.lot_size * self.qty_lots

    @property
    def cost(self) -> float:
        notional = (self.entry_premium + self.exit_premium) * self.lot_size * self.qty_lots
        return notional * (COST_PCT_ROUNDTRIP / 2)  # half on each side

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost

    @property
    def return_pct(self) -> float:
        if self.entry_premium == 0:
            return 0
        return (self.exit_premium - self.entry_premium) / self.entry_premium * 100

    @property
    def hold_minutes(self) -> int:
        return int((self.exit_time - self.entry_time).total_seconds() / 60)


# ----- Premium model ------------------------------------------------------
def model_entry_premium(spot: float) -> float:
    return max(spot * ATM_PREMIUM_PCT_OF_SPOT, 1.0)


def model_premium(entry_premium: float, entry_spot: float, cur_spot: float,
                  direction: str, hours_held: float) -> float:
    """Approximate current option premium given spot move + time decay."""
    if direction == "BUY_CALL":
        intrinsic_change = (cur_spot - entry_spot) * DELTA_ATM
    else:
        intrinsic_change = (entry_spot - cur_spot) * DELTA_ATM
    # Theta decay (intraday is small)
    decay = entry_premium * THETA_PER_HOUR_PCT * hours_held
    premium = entry_premium + intrinsic_change - decay
    return max(premium, 0.5)   # floor to avoid going to zero / negative


# ----- Signal scoring (mirrors options_signal.generate_signal) -------------
def score_setup(row, history: pd.DataFrame) -> Tuple[str, int, str]:
    """Returns (direction, score, reason).
    direction: 'BUY_CALL' / 'BUY_PUT' / 'NO_TRADE'
    score:     0-5 (number of confirmations)
    """
    # VWAP often NaN for indices (yfinance reports volume=0) — make it optional
    required = ("ema9", "ema21", "ema50", "rsi", "macd", "macd_signal",
                "supertrend_dir")
    if any(pd.isna(row[c]) for c in required):
        return ("NO_TRADE", 0, "warmup")

    spot = row["close"]
    bull, bear = 0, 0
    bull_r, bear_r = [], []

    # 1. Price vs VWAP — but VWAP often NaN for indices (no volume). Skip if NaN.
    if not pd.isna(row["vwap"]) and row["vwap"] > 0:
        if spot > row["vwap"]:
            bull += 1; bull_r.append("spot>VWAP")
        elif spot < row["vwap"]:
            bear += 1; bear_r.append("spot<VWAP")

    # 2. EMA alignment
    e9, e21, e50 = row["ema9"], row["ema21"], row["ema50"]
    if e9 > e21 > e50:
        bull += 1; bull_r.append("EMA9>21>50")
    elif e9 < e21 < e50:
        bear += 1; bear_r.append("EMA9<21<50")

    # 3. RSI zone
    rsi = row["rsi"]
    if 50 < rsi < 70:
        bull += 1; bull_r.append(f"RSI{rsi:.0f}")
    elif 30 < rsi < 50:
        bear += 1; bear_r.append(f"RSI{rsi:.0f}")

    # 4. MACD
    if row["macd"] > row["macd_signal"] and row["macd"] > 0:
        bull += 1; bull_r.append("MACD↑")
    elif row["macd"] < row["macd_signal"] and row["macd"] < 0:
        bear += 1; bear_r.append("MACD↓")

    # 5. Supertrend
    if row["supertrend_dir"] == 1:
        bull += 1; bull_r.append("ST↑")
    elif row["supertrend_dir"] == -1:
        bear += 1; bear_r.append("ST↓")

    # Threshold: 4 if VWAP contributed (5 possible), else 3 (only 4 possible).
    vwap_contributed = (not pd.isna(row.get("vwap"))) and row["vwap"] > 0
    threshold = 4 if vwap_contributed else 3
    if bull >= threshold and bull > bear:
        return ("BUY_CALL", bull, " + ".join(bull_r))
    if bear >= threshold and bear > bull:
        return ("BUY_PUT", bear, " + ".join(bear_r))
    return ("NO_TRADE", max(bull, bear), "mixed")


# ----- Simulator -----------------------------------------------------------
def fetch_spot_history(yf_symbol: str, days: int) -> Optional[pd.DataFrame]:
    days = min(days, 60)
    df = yf.Ticker(yf_symbol).history(period=f"{days}d", interval="5m",
                                       auto_adjust=False)
    if df is None or df.empty:
        return None
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    df.index.name = "date"
    df = df[(df.index.time >= TRADING_OPEN) & (df.index.time <= TRADING_CLOSE)]
    return df


def simulate(under: Underlying, df: pd.DataFrame,
             stop_pct: float = 0.004, target_pct: float = 0.008,
             skip_first_min: int = 0,         # skip first N minutes after open
             stop_new_entries_at: Optional[dtime] = None,  # cutoff for new entries
             min_adx: float = 0.0,            # require ADX above this (0 = off)
             strict_threshold: bool = False,  # require ALL confirmations available
             cooldown_sec: int = 0,           # lockout after a WIN exit (0 = off)
             cooldown_loss_sec: int = 0,      # lockout after a LOSING exit (0 = off)
             max_premium_above_open: float = 0.0,  # premium-chase guard (0 = off)
             use_rupee_exits: bool = False,   # mirror live bot: rupee quick-loss + ratchet
             quick_loss_rs: float = 150.0,    # per-lot rupee stop (rupee mode)
             quick_profit_rs: float = 300.0,  # per-lot ratchet arm (rupee mode)
             ratchet_step_rs: float = 50.0,   # per-lot ratchet step (rupee mode)
             breakeven_arm_rs: float = 0.0,   # per-lot: arm breakeven stop at this profit (0=off)
             ) -> List[OptionTrade]:
    """One-pass walk-forward simulation. stop/target are in spot fractions.

    Extra filters (default = original behavior):
      skip_first_min       — skip new entries for N minutes after market open
      stop_new_entries_at  — cutoff time for new entries (e.g. 14:30)
      min_adx              — only enter if ADX >= this threshold
      strict_threshold     — require 4-of-4 (no VWAP fallback to 3-of-3)
      cooldown_sec         — block re-entry for N sec after a winning exit
      cooldown_loss_sec    — block re-entry for N sec after a losing exit
                             (effective post-loss lockout = max of the two,
                              mirroring the live bot's can_open() logic)
      max_premium_above_open — block entry if the option premium has run up
                             more than this fraction above its value at the
                             day's open (e.g. 0.30 = the live bot's 30% guard).
                             Modelled by re-pricing the same contract at the
                             day-open spot via the delta model. 0 = disabled.
    """
    df = ind.compute_all(df)
    trades: List[OptionTrade] = []
    if stop_new_entries_at is None:
        stop_new_entries_at = dtime(14, 55)
    entry_open_cutoff = stop_new_entries_at

    in_position = False
    direction = ""
    entry_premium = entry_spot = stop_spot = target_spot = 0.0
    entry_time: Optional[datetime] = None
    reason_entry = ""
    # Cooldown state — when we last exited and whether it was a loss. Overnight
    # gaps far exceed any cooldown, so this naturally never blocks next-day entries.
    last_exit_dt: Optional[datetime] = None
    last_exit_was_loss = False
    # Premium-chase guard state — the day's opening spot, reset each new date.
    cur_date = None
    day_open_spot = 0.0
    # Rupee-exit state (live-bot mirror): ratchet + breakeven, reset on each entry.
    trail_armed = False
    be_armed = False
    peak_profit_rs = 0.0
    lock_floor_rs = 0.0

    # Warm-up: need >= 50 bars for EMA50 + ADX
    for i in range(60, len(df) - 1):
        now = df.index[i].to_pydatetime()
        row = df.iloc[i]
        next_row = df.iloc[i + 1]
        history = df.iloc[max(0, i - 5):i + 1]

        # Track the day's opening spot for the premium-chase guard.
        if now.date() != cur_date:
            cur_date = now.date()
            day_open_spot = float(row["open"])

        # ----- Manage open position -----
        if in_position:
            hours_held = (now - entry_time).total_seconds() / 3600.0
            cur_premium = model_premium(entry_premium, entry_spot,
                                         row["close"], direction, hours_held)

            exit_premium = None
            exit_reason = None
            # Squareoff first
            if next_row.name.to_pydatetime().time() >= SQUAREOFF_TIME:
                exit_premium = model_premium(entry_premium, entry_spot,
                                              next_row["open"], direction,
                                              hours_held)
                exit_reason = "SQUAREOFF_315PM"
            # Catastrophic premium stop
            elif cur_premium <= entry_premium * (1 - CATASTROPHIC_STOP_PCT):
                exit_premium = entry_premium * (1 - CATASTROPHIC_STOP_PCT)
                exit_reason = "CATASTROPHIC_PREMIUM_STOP"
            elif use_rupee_exits:
                # Live-bot mirror: rupee quick-loss + quick-profit ratchet (+ optional
                # breakeven stop). Evaluated on the bar-close premium.
                unreal = (cur_premium - entry_premium) * under.lot_size
                # 1) Quick-profit ratchet (let winners run, lock in steps)
                if quick_profit_rs > 0:
                    if not trail_armed and unreal >= quick_profit_rs:
                        trail_armed = True
                        peak_profit_rs = unreal
                        lock_floor_rs = quick_profit_rs
                    if trail_armed:
                        if unreal > peak_profit_rs:
                            peak_profit_rs = unreal
                        steps = (int((peak_profit_rs - quick_profit_rs) // ratchet_step_rs)
                                 if ratchet_step_rs > 0 else 0)
                        lock_floor_rs = quick_profit_rs + steps * ratchet_step_rs
                        if unreal < lock_floor_rs:
                            exit_premium = cur_premium
                            exit_reason = "QUICK_PROFIT_RATCHET"
                # 2) Breakeven stop — once modestly green, don't let it become a loss
                #    (only relevant before the ratchet arms)
                if exit_reason is None and breakeven_arm_rs > 0 and not trail_armed:
                    if not be_armed and unreal >= breakeven_arm_rs:
                        be_armed = True
                    if be_armed and unreal < 0:
                        exit_premium = cur_premium
                        exit_reason = "BREAKEVEN_STOP"
                # 3) Quick-loss hard cap
                if exit_reason is None and quick_loss_rs > 0 and unreal <= -abs(quick_loss_rs):
                    exit_premium = cur_premium
                    exit_reason = "QUICK_LOSS"
                # 4) Signal-flip exit
                if exit_reason is None:
                    new_dir, _, _ = score_setup(row, history)
                    if new_dir != "NO_TRADE" and new_dir != direction:
                        exit_premium = cur_premium
                        exit_reason = "SIGNAL_FLIPPED"
            # Spot-based stop hit (intra-bar via worst-case high/low)
            elif direction == "BUY_CALL" and row["low"] <= stop_spot:
                exit_premium = model_premium(entry_premium, entry_spot,
                                              stop_spot, direction, hours_held)
                exit_reason = "STOP_HIT"
            elif direction == "BUY_PUT" and row["high"] >= stop_spot:
                exit_premium = model_premium(entry_premium, entry_spot,
                                              stop_spot, direction, hours_held)
                exit_reason = "STOP_HIT"
            # Target hit
            elif direction == "BUY_CALL" and row["high"] >= target_spot:
                exit_premium = model_premium(entry_premium, entry_spot,
                                              target_spot, direction, hours_held)
                exit_reason = "TARGET_HIT"
            elif direction == "BUY_PUT" and row["low"] <= target_spot:
                exit_premium = model_premium(entry_premium, entry_spot,
                                              target_spot, direction, hours_held)
                exit_reason = "TARGET_HIT"
            # Signal-flip exit (re-score; opposite direction triggers)
            else:
                new_dir, new_score, _ = score_setup(row, history)
                if new_dir != "NO_TRADE" and new_dir != direction:
                    exit_premium = cur_premium
                    exit_reason = "SIGNAL_FLIPPED"

            if exit_reason:
                trades.append(OptionTrade(
                    underlying=under.name,
                    direction=direction,
                    entry_time=entry_time,
                    exit_time=next_row.name.to_pydatetime(),
                    entry_spot=entry_spot,
                    exit_spot=next_row["open"],
                    entry_premium=entry_premium,
                    exit_premium=exit_premium,
                    lot_size=under.lot_size,
                    stop_spot=stop_spot,
                    target_spot=target_spot,
                    reason_entry=reason_entry,
                    reason_exit=exit_reason,
                ))
                in_position = False
                # Start cooldown clock for the next entry on this underlying.
                last_exit_dt = next_row.name.to_pydatetime()
                last_exit_was_loss = (trades[-1].net_pnl <= 0)

        # ----- Try entry -----
        # Compute first-entry-allowed time once
        first_entry_time = dtime(
            (TRADING_OPEN.hour * 60 + TRADING_OPEN.minute + skip_first_min) // 60,
            (TRADING_OPEN.hour * 60 + TRADING_OPEN.minute + skip_first_min) % 60,
        )
        if (not in_position
                and now.time() >= first_entry_time
                and now.time() < entry_open_cutoff):
            # Cooldown gate — block re-entry within the lockout window after an
            # exit. Post-loss uses max(win, loss) cooldown, matching the live bot.
            if last_exit_dt is not None:
                cd = (max(cooldown_sec, cooldown_loss_sec) if last_exit_was_loss
                      else cooldown_sec)
                if cd > 0 and (now - last_exit_dt).total_seconds() < cd:
                    continue
            # Optional ADX filter (uses indicator already computed)
            if min_adx > 0:
                cur_adx = row.get("adx")
                if pd.isna(cur_adx) or cur_adx < min_adx:
                    continue
            new_dir, score, reason = score_setup(row, history)
            # Strict mode: only accept score>=4 with VWAP contribution (no 3-of-3 fallback)
            min_score = 4
            if strict_threshold:
                vwap_present = (not pd.isna(row.get("vwap"))) and row["vwap"] > 0
                if not vwap_present:
                    continue  # skip when VWAP unavailable
            if new_dir in ("BUY_CALL", "BUY_PUT") and score >= min_score:
                entry_spot = float(next_row["open"])
                entry_premium = model_entry_premium(entry_spot)
                # Premium-chase guard: re-price this contract at the day's open
                # spot; skip if the premium has run up > limit since then.
                if max_premium_above_open > 0 and day_open_spot > 0:
                    prem_open = model_premium(entry_premium, entry_spot,
                                              day_open_spot, new_dir, 0.0)
                    if prem_open > 0 and (
                            (entry_premium - prem_open) / prem_open
                            > max_premium_above_open):
                        continue
                entry_time = next_row.name.to_pydatetime()
                direction = new_dir
                reason_entry = reason
                if direction == "BUY_CALL":
                    stop_spot = entry_spot * (1 - stop_pct)
                    target_spot = entry_spot * (1 + target_pct)
                else:
                    stop_spot = entry_spot * (1 + stop_pct)
                    target_spot = entry_spot * (1 - target_pct)
                in_position = True
                # Reset rupee-exit state for the new position.
                trail_armed = False
                be_armed = False
                peak_profit_rs = 0.0
                lock_floor_rs = 0.0

    # End of data
    if in_position:
        last = df.iloc[-1]
        hours_held = (last.name.to_pydatetime() - entry_time).total_seconds() / 3600.0
        final_prem = model_premium(entry_premium, entry_spot, last["close"],
                                    direction, hours_held)
        trades.append(OptionTrade(
            underlying=under.name,
            direction=direction,
            entry_time=entry_time,
            exit_time=last.name.to_pydatetime(),
            entry_spot=entry_spot,
            exit_spot=last["close"],
            entry_premium=entry_premium,
            exit_premium=final_prem,
            lot_size=under.lot_size,
            stop_spot=stop_spot,
            target_spot=target_spot,
            reason_entry=reason_entry,
            reason_exit="END_OF_DATA",
        ))
    return trades


# ----- Report --------------------------------------------------------------
def report(trades: List[OptionTrade], label: str):
    if not trades:
        print(f"\n{label}: NO TRADES")
        return
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    total = sum(t.net_pnl for t in trades)
    avg_w = np.mean([t.net_pnl for t in wins]) if wins else 0
    avg_l = np.mean([t.net_pnl for t in losses]) if losses else 0
    best = max(trades, key=lambda t: t.net_pnl)
    worst = min(trades, key=lambda t: t.net_pnl)
    pf = (sum(t.net_pnl for t in wins) / abs(sum(t.net_pnl for t in losses))
          if losses else float("inf"))
    # Exit-reason breakdown
    from collections import Counter
    reasons = Counter(t.reason_exit for t in trades)

    print()
    print("─" * 78)
    print(f"  {label}")
    print("─" * 78)
    print(f"  Trades       : {len(trades)}   "
          f"(Wins {len(wins)} / Losses {len(losses)})")
    print(f"  Win rate     : {len(wins)/len(trades)*100:.1f}%")
    print(f"  Total P&L    : Rs {total:+,.0f}  "
          f"(modelled, 1 lot per trade)")
    print(f"  Avg win      : Rs {avg_w:+,.0f}      Avg loss: Rs {avg_l:+,.0f}")
    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
    print(f"  Profit factor: {pf_str}")
    print(f"  Best trade   : {best.underlying} {best.entry_time.strftime('%m-%d %H:%M')} "
          f"{best.direction} Rs {best.net_pnl:+,.0f} ({best.reason_exit})")
    print(f"  Worst trade  : {worst.underlying} {worst.entry_time.strftime('%m-%d %H:%M')} "
          f"{worst.direction} Rs {worst.net_pnl:+,.0f} ({worst.reason_exit})")
    print(f"  Exits        : {dict(reasons)}")


def write_csv(trades: List[OptionTrade], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "underlying", "direction", "entry_time", "exit_time",
            "entry_spot", "exit_spot", "entry_premium", "exit_premium",
            "lot_size", "gross_pnl", "cost", "net_pnl", "return_pct",
            "hold_min", "reason_entry", "reason_exit",
        ])
        for t in sorted(trades, key=lambda x: x.entry_time):
            w.writerow([
                t.underlying, t.direction,
                t.entry_time.isoformat(timespec="seconds"),
                t.exit_time.isoformat(timespec="seconds"),
                round(t.entry_spot, 2), round(t.exit_spot, 2),
                round(t.entry_premium, 2), round(t.exit_premium, 2),
                t.lot_size,
                round(t.gross_pnl, 2), round(t.cost, 2),
                round(t.net_pnl, 2), round(t.return_pct, 2),
                t.hold_minutes,
                t.reason_entry, t.reason_exit,
            ])


# ----- Main ----------------------------------------------------------------
# ----- Pre-defined configurations to sweep ------------------------------
SWEEP_CONFIGS = {
    "baseline": dict(
        stop_pct=0.004, target_pct=0.008,
        skip_first_min=0, stop_new_entries_at=dtime(14, 55),
        min_adx=0.0, strict_threshold=False,
    ),
    "skip-open-30m": dict(
        stop_pct=0.004, target_pct=0.008,
        skip_first_min=30, stop_new_entries_at=dtime(14, 55),
        min_adx=0.0, strict_threshold=False,
    ),
    "adx-22": dict(
        stop_pct=0.004, target_pct=0.008,
        skip_first_min=0, stop_new_entries_at=dtime(14, 55),
        min_adx=22.0, strict_threshold=False,
    ),
    "tight-target": dict(
        stop_pct=0.004, target_pct=0.006,    # 1.5:1 R:R, more wins
        skip_first_min=0, stop_new_entries_at=dtime(14, 55),
        min_adx=0.0, strict_threshold=False,
    ),
    "combo-skip+adx": dict(
        stop_pct=0.004, target_pct=0.007,
        skip_first_min=30, stop_new_entries_at=dtime(14, 30),
        min_adx=20.0, strict_threshold=False,
    ),
    "combo-all-filters": dict(
        stop_pct=0.003, target_pct=0.006,    # tighter both sides
        skip_first_min=45, stop_new_entries_at=dtime(14, 15),
        min_adx=25.0, strict_threshold=False,
    ),
}


def main():
    p = argparse.ArgumentParser(description="Options strategy backtest (delta-approximation).")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "SENSEX"], default=None)
    p.add_argument("--sweep", action="store_true",
                   help="Test all 6 pre-defined configurations and rank by profit factor")
    p.add_argument("--config", choices=list(SWEEP_CONFIGS.keys()), default=None,
                   help="Run only this configuration")
    args = p.parse_args()

    print()
    print("=" * 78)
    print(f"  OPTIONS BACKTEST  —  {args.days}-day 5-min spot  —  "
          f"delta-approximation model")
    print("=" * 78)
    print(f"  Model: entry premium = 0.5% of spot  |  delta = 0.5  |  "
          f"theta = -0.05%/hr")
    print(f"  Stops: spot -0.4% (CALL) / +0.4% (PUT)  |  Target: ±0.8% (2:1 R:R)")
    print(f"  Cat. premium stop: -60%  |  Costs (round-trip): 0.6%")
    print(f"  Sizing: 1 lot per trade (NIFTY=75, BANKNIFTY=30, SENSEX=20)")

    actives = [u for u in UNDERLYINGS if args.underlying is None or u.name == args.underlying]

    # Cache the fetched data so we don't re-download for each config
    cache = {}
    for u in actives:
        print(f"\n[{u.name}]  Fetching {u.yf_symbol}...", end=" ", flush=True)
        df = fetch_spot_history(u.yf_symbol, args.days)
        if df is None or len(df) < 100:
            print("  → SKIPPED (no data)")
            continue
        print(f"  → {len(df)} bars  ({df.index[0].date()} to {df.index[-1].date()})")
        cache[u.name] = (u, df)

    if not cache:
        print("\nNo data — aborting.")
        return

    # Determine which configs to run
    if args.sweep:
        configs_to_run = SWEEP_CONFIGS
    elif args.config:
        configs_to_run = {args.config: SWEEP_CONFIGS[args.config]}
    else:
        configs_to_run = {"baseline": SWEEP_CONFIGS["baseline"]}

    sweep_results = []

    for cfg_name, cfg in configs_to_run.items():
        print()
        print("=" * 78)
        print(f"  CONFIG: {cfg_name}   {cfg}")
        print("=" * 78)
        all_trades = []
        for name, (u, df) in cache.items():
            trades = simulate(u, df, **cfg)
            if len(configs_to_run) == 1:
                report(trades, label=u.name)
            all_trades.extend(trades)

        # Aggregate report for this config
        report(all_trades, label=f"ALL ({cfg_name})")

        # Capture summary for ranking
        if all_trades:
            wins = sum(1 for t in all_trades if t.net_pnl > 0)
            total = sum(t.net_pnl for t in all_trades)
            wpnl = sum(t.net_pnl for t in all_trades if t.net_pnl > 0)
            lpnl = sum(t.net_pnl for t in all_trades if t.net_pnl <= 0)
            pf = wpnl / abs(lpnl) if lpnl else float("inf")
            sweep_results.append({
                "config": cfg_name,
                "trades": len(all_trades),
                "win_rate": round(wins / len(all_trades) * 100, 1),
                "total_pnl": round(total, 0),
                "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
            })

    # Comparison ranking
    if args.sweep and len(sweep_results) > 1:
        print()
        print("=" * 78)
        print("  RANKING (best to worst by profit factor)")
        print("=" * 78)
        # Sort by profit_factor desc (handle "inf" string)
        def pf_key(r):
            pf = r["profit_factor"]
            return float("inf") if pf == "inf" else pf
        for i, r in enumerate(sorted(sweep_results, key=pf_key, reverse=True), 1):
            print(f"  {i}. {r['config']:<22} trades={r['trades']:>4}   "
                  f"win={r['win_rate']:>4.1f}%   "
                  f"P&L=Rs {r['total_pnl']:+>8,.0f}   PF={r['profit_factor']}")

    # Use last config's trades for output
    all_trades = []
    for name, (u, df) in cache.items():
        last_cfg = list(configs_to_run.values())[-1]
        all_trades.extend(simulate(u, df, **last_cfg))

    out = "logs/backtest_options.csv"
    write_csv(all_trades, out)
    print(f"\n  Trade log: {out}")
    print()
    print("  CAVEAT:")
    print("    * Premium changes modelled via delta=0.5 (ATM). Real ITM/OTM differ.")
    print("    * Theta = 0.05%/hr is conservative — real intraday theta is small.")
    print("    * No IV crush modelled. Expect ±20-30% variance in live ₹ P&L.")
    print("    * Bid-ask spreads on SENSEX OTM strikes can be 5-10% — eats winners.")
    print()


if __name__ == "__main__":
    main()
