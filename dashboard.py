"""
dashboard.py
------------
Live terminal dashboard built with `rich`. Renders three panels:

  ┌─────────── Header (clock, capital, halt status) ────────────┐
  ├──────────── Open Positions (live mark-to-market) ───────────┤
  ├────────────── Indicator Snapshot per Symbol ────────────────┤
  └────────────────── Recent Signals Fired ─────────────────────┘

Usage from main.py:

    dash = Dashboard(feed, risk)
    dash.start()
    ...
    dash.note_signal(signal)        # call when strategy fires
    dash.refresh(indicator_snapshots)
    ...
    dash.stop()
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, Dict, Optional

import pytz
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config

IST = pytz.timezone("Asia/Kolkata")


class Dashboard:
    def __init__(self, feed, risk, max_signals: int = 15) -> None:
        self.feed = feed
        self.risk = risk
        self.signals: Deque[dict] = deque(maxlen=max_signals)
        self.indicator_snapshots: Dict[str, dict] = {}
        self._live: Optional[Live] = None

    # ------------------------------------------------------------------ #
    # Public — called by main loop
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._live = Live(self._render(), refresh_per_second=2, screen=False)
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def note_signal(self, signal) -> None:
        """Record a signal so it shows up in the 'Recent Signals' panel."""
        self.signals.appendleft({
            "ts": datetime.now(IST).strftime("%H:%M:%S"),
            "symbol": signal.symbol,
            "type": signal.type.value,
            "price": signal.price,
            "reason": signal.reason,
        })

    def update_indicators(self, symbol: str, snapshot: dict) -> None:
        self.indicator_snapshots[symbol] = snapshot

    def refresh(self) -> None:
        if self._live:
            self._live.update(self._render())

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def _render(self) -> Group:
        return Group(
            self._header_panel(),
            self._positions_panel(),
            self._indicators_panel(),
            self._signals_panel(),
        )

    def _header_panel(self) -> Panel:
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        realized = self.risk.daily.realized_pnl
        unreal = self.risk.unrealized_pnl(self.feed.last_prices)
        total = realized + unreal

        halt_txt = ""
        if self.risk.daily.halted:
            halt_txt = f"  [bold red]HALTED[/bold red] ({self.risk.daily.halt_reason})"
        dry_txt = "  [yellow][DRY-RUN][/yellow]" if config.EXECUTION.dry_run else ""

        body = Text.assemble(
            (f"{now}   ", "bold white"),
            (f"Capital: ₹{config.RISK.capital:,.0f}   ", "white"),
            (f"Realized: ", "white"),
            (f"₹{realized:,.0f}", "green" if realized >= 0 else "red"),
            (f"   Unrealized: ", "white"),
            (f"₹{unreal:,.0f}", "green" if unreal >= 0 else "red"),
            (f"   Total: ", "bold white"),
            (f"₹{total:,.0f}", "bold green" if total >= 0 else "bold red"),
            (f"   Trades: {self.risk.daily.trades_today}", "white"),
            (halt_txt + dry_txt, ""),
        )
        return Panel(body, title="[bold]Intraday Bot[/bold]", border_style="cyan")

    def _positions_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold magenta",
                      expand=True, pad_edge=False)
        for col in ("Symbol", "Qty", "Entry", "LTP", "SL", "Target", "P&L"):
            table.add_column(col, justify="right" if col != "Symbol" else "left")

        for sym, pos in self.risk.positions.items():
            ltp = self.feed.last_prices.get(sym, pos.entry_price)
            pnl = (ltp - pos.entry_price) * pos.quantity
            pnl_color = "green" if pnl >= 0 else "red"
            table.add_row(
                sym,
                str(pos.quantity),
                f"{pos.entry_price:.2f}",
                f"{ltp:.2f}",
                f"{pos.stop_loss:.2f}",
                f"{pos.target:.2f}",
                f"[{pnl_color}]₹{pnl:,.0f}[/{pnl_color}]",
            )
        if not self.risk.positions:
            table.add_row("[dim]— no open positions —[/dim]", "", "", "", "", "", "")
        return Panel(table, title="Open Positions", border_style="blue")

    def _indicators_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold magenta",
                      expand=True, pad_edge=False)
        for col in ("Symbol", "LTP", "RSI", "MACD", "EMA9", "EMA21", "EMA50",
                    "VWAP", "ST", "Pattern"):
            table.add_column(col, justify="right" if col != "Symbol" else "left")

        for sym in self.feed.symbol_to_token:
            snap = self.indicator_snapshots.get(sym, {})
            ltp = self.feed.last_prices.get(sym)
            if not snap and ltp is None:
                continue

            def fmt(v, nd=2):
                return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"

            st_dir = snap.get("supertrend_dir", 0)
            st_str = "[green]↑[/green]" if st_dir == 1 else "[red]↓[/red]" if st_dir == -1 else "—"

            table.add_row(
                sym,
                fmt(ltp),
                fmt(snap.get("rsi"), 1),
                fmt(snap.get("macd"), 3),
                fmt(snap.get("ema9")),
                fmt(snap.get("ema21")),
                fmt(snap.get("ema50")),
                fmt(snap.get("vwap")),
                st_str,
                str(snap.get("pattern", "")),
            )
        return Panel(table, title="Indicators (primary interval)", border_style="green")

    def _signals_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold magenta",
                      expand=True, pad_edge=False)
        for col in ("Time", "Symbol", "Signal", "Price", "Reason"):
            table.add_column(col)

        for s in list(self.signals):
            sig_color = {"BUY": "green", "EXIT": "yellow"}.get(s["type"], "white")
            table.add_row(
                s["ts"],
                s["symbol"],
                f"[{sig_color}]{s['type']}[/{sig_color}]",
                f"{s['price']:.2f}",
                s["reason"],
            )
        if not self.signals:
            table.add_row("[dim]— no signals yet —[/dim]", "", "", "", "")
        return Panel(table, title="Recent Signals", border_style="yellow")
