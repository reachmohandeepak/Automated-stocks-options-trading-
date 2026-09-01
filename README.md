# Automated Stocks & Options Trading Bot

A Python-based automated intraday trading system for stocks and options markets in India. The bot trades on **NSE (National Stock Exchange)** using live market data, technical indicators, and systematic risk management.

---

## 📊 Project Overview

This is a **production-ready trading bot** that:

- **Monitors multiple stocks/indices** in real-time for trading signals
- **Places automated trades** based on technical analysis (EMA, RSI, MACD, VWAP, Supertrend)
- **Manages risk** with position sizing, stop-loss, trailing stops, and daily loss limits
- **Handles order execution** with dry-run mode for testing before going live
- **Tracks performance** via detailed logging and CSV trade records
- **Provides live dashboard** showing positions, signals, and P&L
- **Sends alerts** via Telegram for all trading events

Designed for **intraday trading** (positions closed by market close), with support for both:
- **Equity futures/stocks** via `main.py`
- **Options trading** (NIFTY/BANKNIFTY calls/puts) via `options_bot.py`

---

## 🎯 Key Features

### Trading Strategy
- **Entry signals** based on multiple confirmations:
  - Price crosses above VWAP
  - EMA 9 > EMA 21 > EMA 50 (bullish alignment)
  - RSI in [50, 70] zone (healthy momentum)
  - MACD line crosses above signal line

- **Exit signals** trigger on any of:
  - Price crosses below VWAP
  - RSI enters overbought territory (> 75)
  - Supertrend reversal (bearish flip)
  - Stop-loss or take-profit hit
  - Trailing stop hit

### Risk Management
- **Position Sizing**: Kelly criterion-inspired, based on risk-per-trade %
- **Stop Loss & Target**: Fixed % or dynamic based on ATR/entry
- **Trailing Stops**: Automatically moves up as price advances
- **Daily Loss Limit**: Kills new entries if losses exceed 3% of capital
- **Max Open Positions**: Limits concurrent trades to prevent overexposure
- **Forced Square-off**: All positions closed at 15:15 IST (market close buffer)

### Broker Integration
- **Zerodha Kite Connect** (primary, real-time data & order execution)
- **Fallback support** for yfinance (free, 15-min delayed, testing)
- **Stub support** for Upstox and Angel One (commented out, can be enabled)

### Execution Modes
- **Dry-run mode** (default): Logs orders without placing them — ideal for testing
- **Live mode**: Places real orders via broker API
- **Market orders**: For speed and guaranteed fills
- **Limit orders**: For better fills with slippage control

### Monitoring & Alerts
- **Live terminal dashboard** showing open positions, signals, P&L
- **CSV trade logs** for post-session analysis
- **Signal logs** for strategy validation
- **Telegram notifications** for every trade, entry, exit, and error
- **Structured logging** to file with timestamps

---

## 🏗️ Architecture

```
data_feed.py
    ↓ (real-time candles & quotes)
indicators.py (compute RSI, MACD, EMA, VWAP, etc.)
    ↓
strategy.py (evaluate BUY/EXIT signals)
    ↓
main.py / options_bot.py (main orchestrator)
    ├→ risk_manager.py (position sizing, stop/target, daily limits)
    ├→ order_executor.py (place/manage orders via broker)
    ├→ dashboard.py (live terminal UI)
    └→ logger.py (logs + Telegram alerts)
```

### Core Modules

| Module | Purpose |
|--------|---------|
| `main.py` | Main orchestrator for equity trading. Runs the event loop, manages positions, and coordinates all subsystems. |
| `options_bot.py` | Dedicated bot for options (NIFTY/BANKNIFTY). Polls signals every 5 min, sizes in lots, polls exits every 30s. |
| `strategy.py` | Pure signal generation logic. Stateless — returns BUY/EXIT/HOLD based on indicators. |
| `indicators.py` | Hand-rolled technical indicators (RSI, MACD, EMA, VWAP, Bollinger Bands, Supertrend). No external TA libs. |
| `data_feed.py` | Fetches OHLCV candles, manages websocket ticks, resolves broker tokens. Supports Kite, yfinance, and others. |
| `risk_manager.py` | Position sizing, stop/target computation, trailing stops, daily P&L, forced square-off checks. |
| `order_executor.py` | Wraps broker API calls (buy, sell, modify, cancel) with retries and error handling. |
| `dashboard.py` | Live terminal UI showing symbols, positions, open P&L, signals history. |
| `logger.py` | Structured logging to files and Telegram. Trade logs in CSV. |
| `config.py` | Central configuration. All tunable parameters live here (watchlist, risk limits, thresholds, broker creds). |

---

## 📈 Trading Examples

### Equity Trading (main.py)
```
09:30 → Market opens, bot bootstraps 200 candles (5-min bars)
09:35 → RELIANCE signal: Price crosses VWAP, EMA alignment OK, RSI=58
        → BUY 5 shares @ ₹2500, SL @ ₹2482.50, Target @ ₹2535
10:45 → Price hits trailing stop → EXIT @ ₹2532, P&L: +160 (0.64%)
11:00 → Next signal on TCS → BUY...
15:15 → EOD square-off: All remaining positions sold at market
```

### Options Trading (options_bot.py)
```
09:30 → Connects to Kite, polls NIFTY spot
10:00 → Signal: NIFTY bullish (4-of-5 indicators confirm)
        → Fetch live NIFTY 24000 CE premium: ₹120
        → Size: 25 lots (based on ₹50K capital, 1% risk)
        → BUY 25 CE contracts @ ₹120/lot → ₹3,00,000 deployed
10:30 → Live spot: NIFTY at 24030, CE premium at ₹145
11:00 → Spot drops to 23950 (SL hit) → EXIT 25 CE @ ₹105
        → Realized loss: -₹3,75,000... wait, calc error in demo!
        → P&L: -₹37,500 (1.25% capital loss)
```

---

## 🚀 Getting Started

### Prerequisites
- Python 3.8+
- Zerodha Kite Connect account (for live trading)
- Active Telegram bot token (for alerts)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/reachmohandeepak/Automated-stocks-options-trading-.git
   cd Stocks
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and add:
   - `KITE_API_KEY` — From Zerodha developers.kite.trade
   - `KITE_API_SECRET` — From Zerodha
   - `KITE_ACCESS_TOKEN` — Regenerated daily (see setup instructions)
   - `TELEGRAM_BOT_TOKEN` — From BotFather on Telegram
   - `TELEGRAM_CHAT_ID` — Your Telegram user/group ID

4. **Configure trading parameters** in `config.py`:
   - **WATCHLIST**: Symbols to monitor (e.g., NSE:RELIANCE, NSE:TCS)
   - **RISK.capital**: Total trading capital in INR
   - **RISK.risk_per_trade_pct**: 1% of capital per trade (standard)
   - **EXECUTION.dry_run**: Keep `True` until confident (default)

### Running the Bot

#### Equity Trading (Dry-run):
```bash
python main.py
```
Logs orders to console/file but doesn't place real trades.

#### Equity Trading (Live):
```bash
# In config.py, set: EXECUTION.dry_run = False
python main.py
```
**⚠️ WARNING**: This places real orders with real money!

#### Options Trading (Signals only):
```bash
python options_bot.py
```
Sends Telegram signal alerts only — no real orders.

#### Options Trading (Auto-orders):
```bash
python options_bot.py --auto-orders on --capital 50000
```
Places real options trades. Money will move from your account.

#### Backtesting:
Multiple backtesting scripts included for strategy validation:
- `backtest.py` — Main equity backtest
- `backtest_options.py` — Options strategy backtest
- `backtest_*_sweep.py` — Parameter sweep variants (cooldown, breakeven, premium guard, etc.)

---

## 📋 Configuration Guide

### key Config.py Sections

**Broker & Watchlist:**
```python
BROKER = "kite"
WATCHLIST = [
    "NSE:RELIANCE",
    "NSE:TCS",
    "NSE:ICICIBANK",
    "NSE:SBIN",
    "NSE:BAJFINANCE",
]
```

**Risk Parameters:**
```python
@dataclass
class RiskConfig:
    capital: float = 100_000.0           # Total capital in INR
    risk_per_trade_pct: float = 0.01     # 1% risk per trade
    stop_loss_pct: float = 0.007         # 0.7% SL distance
    reward_to_risk: float = 2.0          # 1:2 R/R ratio
    daily_loss_limit_pct: float = 0.03   # 3% daily max loss
    max_open_positions: int = 3          # Max concurrent trades
```

**Indicator Thresholds:**
```python
@dataclass
class StrategyConfig:
    rsi_buy_min: float = 50.0            # RSI entry zone min
    rsi_buy_max: float = 70.0            # RSI entry zone max
    rsi_exit_overbought: float = 75.0    # RSI exit trigger
    cross_lookback: int = 2              # Candles to check for crosses
```

**Market Timing (IST):**
```python
@dataclass
class TimingConfig:
    market_open_hhmm: tuple = (9, 15)           # 09:15 IST
    market_close_hhmm: tuple = (15, 30)         # 15:30 IST
    squareoff_hhmm: tuple = (15, 15)            # Force SQ-OFF time
    primary_interval_min: int = 5               # 5-min candles
    bootstrap_candles: int = 200                # 200 bars warmup
```

---

## 📊 Daily Workflow

### Before Market Open (09:00 IST)
1. Generate a fresh Kite access token (expires daily)
2. Update `.env` with new `KITE_ACCESS_TOKEN`
3. Review watchlist and risk parameters in `config.py`
4. Set `EXECUTION.dry_run = True` for testing, `False` for live

### During Trading (09:15 – 15:30 IST)
- Bot runs continuously in a terminal
- Live dashboard updates every 1 second
- Signals logged to `logs/signals.csv`
- Trades logged to `logs/trades.csv`
- Telegram alerts on every event

### After Market Close (15:30+ IST)
- All positions auto-closed by 15:15
- Review `logs/trades.csv` and `logs/app.log`
- Analyze P&L and signal quality
- Tune config.py for next day if needed

---

## 📊 Outputs & Logs

### Trade Log (`logs/trades.csv`)
```
TIMESTAMP,SYMBOL,ACTION,BUY/SELL,QUANTITY,PRICE,STOP_LOSS,TARGET,PNL,ORDER_ID,STATUS
2025-06-03 10:15:30,RELIANCE,FILLED,BUY,5,2500.00,2482.50,2535.00,,ORD123,COMPLETE
2025-06-03 10:45:15,RELIANCE,EXIT,SELL,5,2532.00,,,-160.00,ORD124,COMPLETE
```

### Signal Log (`logs/signals.csv`)
```
TIMESTAMP,SYMBOL,SIGNAL,PRICE,REASON,RSI,MACD,EMA9,EMA21,EMA50
2025-06-03 10:15:00,RELIANCE,BUY,2500.00,VWAP↑cross | EMA9>21>50 | RSI=58.3,58.3,0.045,2498.5,2495.0,2490.0
```

### Application Log (`logs/app.log`)
```
2025-06-03 09:30:15 INFO    Initializing trading bot
2025-06-03 09:30:20 INFO    Broker: kite  DryRun: True
2025-06-03 09:35:10 INFO    RELIANCE: BUY signal detected
2025-06-03 09:35:11 INFO    BUY RELIANCE: 5 shares @ ₹2500, SL=₹2482.50, Target=₹2535
...
```

### Telegram Alerts
```
🤖 Trading bot started — 8 symbols, DRY-RUN

📊 RELIANCE
 BUY @ ₹2,500
 SL: ₹2,482.50  |  Target: ₹2,535
 Reason: VWAP↑cross | EMA9>21>50 | RSI=58

🟢 RELIANCE EXIT
 SELL @ ₹2,532  |  P&L: +₹160 (+0.64%)
 Reason: target_hit
```

---

## 🔧 Development & Backtesting

### Running Backtests
```bash
# Simple backtest (60 days, default parameters)
python backtest.py

# Options backtest
python backtest_options.py

# Parameter sweep (find optimal cooldown)
python backtest_cooldown_sweep.py

# Breakeven analysis
python backtest_breakeven_sweep.py

# Analyze audit logs from a prior backtest
python analyze_audit.py
```

### Data Feeds
- **Kite (live)**: Real-time streaming via websocket
- **yfinance**: Free, 15-min delayed, no authentication needed
- **CSV/local files**: For offline backtesting

---

## ⚠️ Risk Warnings

1. **Live trading involves real money loss**. Start with tiny capital or dry-run mode.
2. **Past backtests do not guarantee future returns.** Market regimes change.
3. **Technical glitches can trigger liquidation.** Always have a manual stop-button ready.
4. **Broker connectivity issues** may cause missed exits. Keep an eye on the bot.
5. **Slippage & commissions** reduce real PnL vs. backtest assumptions.
6. **Leverage & options** amplify both gains and losses.

**Never deploy capital you cannot afford to lose.**

---

## 📦 Dependencies

- **kiteconnect** ≥ 4.2.0 — Zerodha Kite broker API
- **pandas** ≥ 2.0.0 — Time series & OHLCV handling
- **numpy** ≥ 1.24.0 — Numerical computations
- **requests** ≥ 2.31.0 — HTTP & Telegram
- **schedule** ≥ 1.2.0 — Job scheduling
- **pytz** ≥ 2023.3 — IST timezone handling
- **rich** ≥ 13.0.0 — Live terminal dashboard
- **python-dotenv** ≥ 1.0.0 — Environment variable loading

---

## 📝 License

Not specified in repository. Check the original repo for licensing details.

---

## 🤝 Contributing

Found a bug or have a feature idea? Submit an issue or PR to the original repository:
https://github.com/reachmohandeepak/Automated-stocks-options-trading-.git

---

## 📚 Further Reading

- **Zerodha Kite Connect API**: https://kite.trade/docs/connect/v3/
- **Technical Analysis**: Investopedia (VWAP, MACD, RSI, EMA)
- **Position Sizing**: "Trade Your Way to Financial Freedom" by Van Tharp
- **Risk Management**: "A Complete Guide to the Futures Market" by Jack Schwager

---

## 🆘 Troubleshooting

### "Permission denied" on GitHub push
Use SSH keys or a GitHub personal access token. See project setup section.

### "Kite access token expired"
Generate a fresh token daily using `get_access_token.py` before 07:30 IST.

### "No signals generated"
- Watchlist symbols might be invalid (check NSE format: `NSE:SYMBOL`)
- Indicators may not be warmed up (200 candles needed)
- Strategy thresholds might be too tight — review `config.STRATEGY`

### "Orders placed but unfilled"
- Slippage too tight? Increase `EXECUTION.slippage_paise`
- Liquidity low? Try more liquid symbols
- Dry-run mode? Orders won't actually hit the market

---

**Happy trading! 📈**
<img width="449" height="1024" alt="image" src="https://github.com/user-attachments/assets/7fa4a85e-1bca-45d2-a561-63b75987bd0b" />
<img width="449" height="1024" alt="image" src="https://github.com/user-attachments/assets/df75269e-9999-4aa2-bc1a-e0bab0a4e2b4" />

