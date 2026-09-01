"""
logger.py
---------
Two responsibilities:
  1. Structured logging — to file + console via the stdlib `logging` module.
  2. Event logging — append rows to CSV files for signals & trades, and push
     real-time alerts to Telegram.

CSV is used (not a database) so the user can open it in Excel mid-session.
"""

import csv
import logging
import os
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Dict, Optional

import pytz
import requests

import config

IST = pytz.timezone("Asia/Kolkata")


# ----------------------------------------------------------------------------
# Stdlib logging setup — file + console handlers
# ----------------------------------------------------------------------------
def _ensure_log_dir() -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)


def get_logger(name: str = "tradingbot") -> logging.Logger:
    """Return a configured logger. Safe to call multiple times — re-uses
    existing handlers so we don't get duplicate log lines."""
    _ensure_log_dir()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler so logs/app.log doesn't grow unbounded
    fh = RotatingFileHandler(config.APP_LOG_FILE, maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


log = get_logger()


# ----------------------------------------------------------------------------
# CSV event logging
# ----------------------------------------------------------------------------
# A lock protects concurrent writes from the websocket thread + main thread.
_csv_lock = threading.Lock()


_SIGNAL_FIELDS = [
    "timestamp_ist", "symbol", "signal", "price", "reason",
    "rsi", "macd", "macd_signal", "ema9", "ema21", "ema50",
    "vwap", "supertrend_dir", "pattern",
]

_TRADE_FIELDS = [
    "timestamp_ist", "symbol", "action", "side", "quantity",
    "price", "order_id", "status", "stop_loss", "target",
    "pnl", "reason",
]


def _append_csv(path: str, fields: list, row: Dict) -> None:
    _ensure_log_dir()
    write_header = not os.path.exists(path)
    with _csv_lock:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            # Fill missing keys with empty string so the row matches header width
            writer.writerow({k: row.get(k, "") for k in fields})


def log_signal(symbol: str, signal: str, price: float, reason: str,
               indicators: Optional[Dict] = None) -> None:
    """Persist a signal evaluation result. Called every time the strategy
    fires a BUY / SELL / EXIT decision (even if we don't act on it)."""
    row = {
        "timestamp_ist": datetime.now(IST).isoformat(timespec="seconds"),
        "symbol": symbol,
        "signal": signal,
        "price": round(price, 2),
        "reason": reason,
    }
    if indicators:
        # Only pull known indicator fields so the CSV stays narrow
        for k in ("rsi", "macd", "macd_signal", "ema9", "ema21", "ema50",
                  "vwap", "supertrend_dir", "pattern"):
            if k in indicators:
                v = indicators[k]
                row[k] = round(v, 4) if isinstance(v, (int, float)) else v
    _append_csv(config.SIGNAL_LOG_CSV, _SIGNAL_FIELDS, row)
    log.info(f"SIGNAL {symbol} {signal} @ {price:.2f} — {reason}")


def log_trade(symbol: str, action: str, side: str, quantity: int,
              price: float, order_id: str = "", status: str = "",
              stop_loss: float = 0.0, target: float = 0.0,
              pnl: float = 0.0, reason: str = "") -> None:
    """Persist an order / fill / exit event.

    action:  "PLACED" | "FILLED" | "REJECTED" | "EXIT" | "SQUAREOFF"
    side:    "BUY"    | "SELL"
    """
    row = {
        "timestamp_ist": datetime.now(IST).isoformat(timespec="seconds"),
        "symbol": symbol,
        "action": action,
        "side": side,
        "quantity": quantity,
        "price": round(price, 2),
        "order_id": order_id,
        "status": status,
        "stop_loss": round(stop_loss, 2),
        "target": round(target, 2),
        "pnl": round(pnl, 2),
        "reason": reason,
    }
    _append_csv(config.TRADE_LOG_CSV, _TRADE_FIELDS, row)
    log.info(
        f"TRADE {action} {side} {symbol} qty={quantity} @ {price:.2f} "
        f"SL={stop_loss:.2f} TGT={target:.2f} pnl={pnl:.2f} — {reason}"
    )


# ----------------------------------------------------------------------------
# Telegram alerts
# ----------------------------------------------------------------------------
def send_telegram(message: str) -> bool:
    """Fire-and-forget Telegram alert. Returns True on HTTP 200.
    Silently no-ops if bot token / chat id are not configured."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code != 200:
            log.warning(f"Telegram non-200: {r.status_code} {r.text}")
            return False
        return True
    except requests.RequestException as e:
        # Network blip — don't crash the trading loop over an alert failure
        log.warning(f"Telegram send failed: {e}")
        return False


def _esc(s) -> str:
    """Escape HTML special chars in user-supplied content (reason strings
    can contain '<' or '>' from indicator descriptions like 'EMA9<21<50').
    Telegram parse_mode=HTML chokes on these without escaping."""
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;"))


def alert_trade(symbol: str, side: str, quantity: int, price: float,
                stop_loss: float, target: float, reason: str) -> None:
    """Formatted Telegram alert for an entry."""
    msg = (
        f"<b>{_esc(side)} {_esc(symbol)}</b>\n"
        f"Qty: {quantity}  @  ₹{price:.2f}\n"
        f"SL: ₹{stop_loss:.2f}   Target: ₹{target:.2f}\n"
        f"Reason: {_esc(reason)}"
    )
    send_telegram(msg)


def alert_exit(symbol: str, quantity: int, price: float, pnl: float,
               reason: str) -> None:
    sign = "🟢" if pnl >= 0 else "🔴"
    msg = (
        f"{sign} <b>EXIT {_esc(symbol)}</b>\n"
        f"Qty: {quantity}  @  ₹{price:.2f}\n"
        f"P&amp;L: ₹{pnl:.2f}\n"
        f"Reason: {_esc(reason)}"
    )
    send_telegram(msg)
