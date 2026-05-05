# JoeBot — Shariah-Compliant AI Trading Bot

An automated stock trading bot that uses **xAI Grok** to scan live news and **Anthropic Claude** to make buy/hold/sell decisions. Runs on **Interactive Brokers (IBKR)** via the TWS API.

Built for a cash account with a Shariah-compliant strategy — no shorting, no margin, no interest-based instruments.

---

## How It Works

Every 15 minutes during market hours the bot runs a cycle:

1. **Grok scans live news** — searches X (Twitter) by cashtag and financial news sites (Reuters, Bloomberg, CNBC, SEC, Stocktwits) for each ticker on the watchlist
2. **Claude scores and decides** — reads Grok's findings and scores each ticker 1–10. Anything scoring ≥7 gets a buy allocation. Claude decides how much USD to put into each qualifying stock.
3. **Orders are placed on IBKR** — market buy orders with automatic OCA-linked stop-loss and take-profit orders. SL/TP are sized using ATR so they scale with how volatile the stock actually is.
4. **News-driven SL management** — every cycle, for held positions, Grok re-scans for negative catalysts. If a strong bearish signal is found (analyst downgrade, earnings warning, breaking bad news), Claude tightens the stop-loss to protect capital. TP is never touched — winners are allowed to run.
5. **EOD logic** — 10 minutes before market close, Grok + Claude decide which positions to hold overnight and which to close. Held positions get fresh GTC SL/TP orders placed before market close.

### AI Architecture (Two-Pass)

```
Pass A — Grok (live search)
  → searches X + web for each ticker
  → returns: BULLISH/BEARISH/NEUTRAL | HIGH/MEDIUM/LOW | top signal

Pass B — Claude (reasoning, no search)
  → reads Pass A output
  → scores each ticker 1-10
  → outputs JSON: conviction scores + USD allocations
```

Scoring rubric:
- **9–10**: Hard news (CNBC/Reuters/SEC) + BULLISH + HIGH volume
- **7–8**: Hard news alone OR BULLISH HIGH from verified sources
- **5–6**: BULLISH MEDIUM, no hard news — skip
- **≤4 / BEARISH / NODATA**: skip

### Risk Controls

- Daily loss limit: 3% of account equity — bot halts for the day if hit
- SL per trade: ATR(14) × 1.5 below entry
- TP per trade: ATR(14) × 2.5 above entry
- Cash buffer: ~5% always kept in cash
- No trades in first 10 minutes after market open
- No trades outside NYSE hours (09:30–16:00 ET, Mon–Fri)
- After a stop-out: 3-cycle cooldown before re-buying the same ticker
- QQQ guard: if Nasdaq is down >1.5% on the day, no new buys

---

## Requirements

- Python 3.12+ (see macOS note below)
- Interactive Brokers account with TWS or IB Gateway
- xAI API key (for Grok)
- Anthropic API key (for Claude)
- Telegram bot token + chat ID (optional, for mobile notifications)

### Install dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install ib_async openai anthropic python-dotenv python-telegram-bot
```

> **macOS Sequoia / Tahoe note:** System Python and Homebrew Python may have a broken `libexpat`. Use pyenv:
> ```bash
> brew install pyenv
> pyenv install 3.12.10
> ~/.pyenv/versions/3.12.10/bin/python -m venv venv
> ```

---

## Setup

### 1. Configure IBKR TWS

In Trader Workstation, go to **Configure → API → Settings**:
- Enable **"Enable ActiveX and Socket Clients"**
- Set socket port to `7496` (live) or `7497` (paper)
- Add `127.0.0.1` to trusted IPs

### 2. Create `.env`

```
IBKR_ACCOUNT_LIVE=U1234567
IBKR_ACCOUNT_PAPER=DU1234567
IBKR_PORT_LIVE=7496
IBKR_PORT_PAPER=7497
IBKR_HOST=127.0.0.1

XAI_API_KEY=xai-...
ANTHROPIC_API_KEY=sk-ant-...

# Optional — for Telegram notifications
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321
```

### 3. Run

```bash
source venv/bin/activate
python main.py
```

The bot auto-launches Trader Workstation if it isn't already open, waits for the API port, then connects and starts trading.

To test your connection first:
```bash
python check.py
```

---

## Configuration (`config.json`)

All parameters are in `config.json`. Changes take effect on the next 15-minute cycle — **no restart needed**.

```json
{
  "mode": "live",
  "watchlist": ["NVDA", "GOOGL", "AAPL", "AMD", "AMZN", "META"],

  "risk": {
    "daily_loss_limit_pct": 0.03,
    "portfolio_cap_usd": 9300,
    "cash_buffer_pct": 0.054,
    "max_single_position_pct": 0.30,
    "cooldown_cycles_after_stopout": 3
  },

  "eod": {
    "close_all_eod": true,
    "close_all_friday": true,
    "close_minutes_before": 10
  },

  "atr": {
    "period": 14,
    "sl_multiplier": 1.5,
    "tp_multiplier": 2.5,
    "sl_update_min_pct": 0.005
  },

  "timing": {
    "cycle_seconds": 900
  },

  "claude": {
    "model": "claude-sonnet-4-6"
  },

  "grok": {
    "model": "grok-4",
    "min_conviction_score": 7,
    "news_lookback_hours": 1,
    "focus_topics": [...],
    "ignore_topics": [...]
  }
}
```

### Key things to customize

| What | Where | How |
|---|---|---|
| Switch to paper trading | `config.json → mode` | `"paper"` |
| Add/remove stocks | `config.json → watchlist` | Add ticker string + add entry to `TICKERS_CONTEXT` in `strategy.py` |
| Raise/lower buy threshold | `config.json → grok.min_conviction_score` | Default 7, raise to 8 for fewer but higher-confidence trades |
| Tighter/wider stops | `config.json → atr.sl_multiplier` | Lower = tighter stop, higher = more room |
| Cycle speed | `config.json → timing.cycle_seconds` | Default 900 (15 min) |
| Claude model | `config.json → claude.model` | Any Anthropic model ID |
| Grok model | `config.json → grok.model` | Any xAI model ID |
| Daily loss limit | `config.json → risk.daily_loss_limit_pct` | `0.03` = 3% of account |

### Adding a new stock

1. Add the ticker to `config.json → watchlist`
2. Add an entry to `TICKERS_CONTEXT` in `strategy.py`:
```python
"TSLA": ("$TSLA", "Tesla Elon Musk EV"),
```
The tuple is `(X cashtag, search keywords)` — used by Grok to find relevant posts.

---

## Project Structure

### `main.py` — Orchestrator
The entry point. Auto-launches Trader Workstation if it isn't open, connects to IBKR, initializes all modules, then runs the 15-minute trading loop. Handles EOD logic: calls the hold/close decision, refreshes overnight SL/TP, and writes the daily summary. Also handles graceful shutdown (press `x + Enter`).

### `config.py` — Configuration
Loads `.env` and `config.json`. Defines `AppContext` — the single object passed to every module — which holds the IBKR connection, xAI/Grok client, and Anthropic/Claude client. `load_config()` is called every cycle so `config.json` changes take effect without restarting.

### `strategy.py` — AI Strategy
The brain. Contains two functions:

- **`get_grok_strategy()`** — called every cycle. Runs the two-pass scan (see Strategy section below) and returns a dict of `{ticker: % of budget}` for qualifying signals.
- **`get_hold_overnight_flags()`** — called at EOD only. Scans news for held positions and asks Claude whether to hold overnight or close. On Fridays uses a deeper 6-hour lookback focused on next-week catalysts and weekend risk.
- **`get_sl_adjustments()`** — called every cycle for held positions. Scans for strong bearish catalysts and returns tighter SL prices if warranted.

To add a new stock, add it to `TICKERS_CONTEXT` at the top of this file with its X cashtag and search keywords.

### `trading.py` — Order Execution
Handles all IBKR order placement:

- **`trade_rebalance()`** — main per-cycle function. Buys new positions, manages trailing stops, trims if significantly over target. Every buy is immediately protected with an OCA-linked SL+TP pair.
- **`close_all_positions()`** — closes everything at EOD or on shutdown, respecting hold-overnight flags.
- **`check_for_stopouts()`** — detects positions that were closed by IBKR (SL or TP fired) by comparing positions cycle-to-cycle. Logs the trade and starts a cooldown.

### `risk.py` — Risk Management
`RiskGuard` class tracks the session:
- Enforces the daily loss limit (halts trading if hit)
- Tracks all open positions and deployed cash in `session_trades.json`
- Manages per-ticker cooldown after a stop-out (default 3 cycles = 45 min)
- Persists state to disk so the bot survives restarts mid-session

### `logger.py` — Logging
Sets up all output:
- `bot_activity.log` — rotating log of everything the bot does
- `orders.log` — human-readable per-trade record with SL, TP, R:R ratio, AI reasoning
- `progress/trades.csv` — one row per closed trade for performance tracking
- Event bus (`emit_event` / `register_event_listener`) — used by Telegram for push notifications

### `account.py` — Account Queries
Two functions: `get_total_capital()` (total portfolio value) and `get_available_cash()`. The cash function uses `SettledCash` not `TotalCashValue` — critical for cash accounts because T+1 means today's sale proceeds can't be reused until tomorrow.

### `atr.py` — Stop/Take-Profit Sizing
Fetches 14 daily bars from IBKR and calculates Average True Range. Used to size SL and TP dynamically based on the stock's actual volatility rather than a fixed percentage.

### `helpers.py` — Market Hours
Stateless time utilities: `is_market_open()`, `is_eod_close_window()`, `is_friday_eod()`, `next_market_open_secs()`. Also runs the keyboard listener for graceful shutdown.

### `telegram_bot.py` — Mobile Control
Runs alongside the main bot. Registers as an event listener so it receives push notifications on every fill. Handles commands (`/status`, `/logs`, `/orders`, `/say`, `/run`, `/stop`).

### `dashboard.py` — GUI Dashboard
Standalone Tkinter GUI. Start/stop the bot service, view the live log, see open positions. Run separately — does not affect trading.

### `check.py` — Connection Test
Run this before starting the bot to verify TWS is reachable and the account ID in `.env` is correct.

### `set_hold_overnight.py` — Manual Hold Override
Run this manually during the day to pre-set which positions should be held overnight. Useful if you want to override the bot's EOD AI decision.

### `config.json` — All Parameters
Every tunable setting. Reloaded every cycle — no restart needed.

---

## Strategy Deep Dive

### Entry (every 15 min)

**Pass A — Grok live search:**
Grok searches X (Twitter) using each ticker's cashtag (e.g. `$NVDA`) and financial news sites. It returns one line per ticker:
```
NVDA | BULLISH | HIGH | Jensen Huang confirms Blackwell demand up 3x | CNBC 10:45 AM
```

**Pass B — Claude reasoning:**
Claude reads the Pass A output and scores each ticker 1–10:

| Score | Meaning |
|---|---|
| 9–10 | Hard news (CNBC/Reuters/SEC) + BULLISH + HIGH confidence |
| 7–8 | Hard news alone OR BULLISH HIGH from verified accounts |
| 5–6 | BULLISH MEDIUM, no hard news — skip |
| ≤4 / BEARISH / NODATA | skip |

Claude then decides how many USD to allocate to each qualifying ticker and outputs JSON. The bot converts this to share counts and places market buy orders.

**Guards:**
- QQQ down >1.5% → no new buys (broad market selloff)
- No free cash → skip Grok scan entirely (saves API cost), just manage existing SL/TP
- First 10 min after open → skip (too volatile)

### Position Management (every cycle)

Every cycle for each held position:
- **Trailing stop**: if price has moved up enough (>0.5%), the stop is raised to lock in gains
- **News SL scan**: Grok re-checks news. If a strong bearish catalyst is found (analyst downgrade with PT cut, earnings warning, SEC filing), Claude tightens the SL. The SL can never be closer than 2% below current price to avoid normal noise stopping you out. TP is never changed.

### Exit

Positions exit in one of four ways:
1. **Stop-loss fires** — IBKR executes the GTC stop order automatically, bot detects it next cycle
2. **Take-profit fires** — same
3. **EOD close** — bot closes non-held positions 10 min before market close
4. **Manual** — press `x + Enter` to close everything and exit

### EOD & Overnight

10 minutes before close, the bot runs a separate Grok+Claude scan for each held position asking whether to hold overnight. Claude weighs the overnight risk vs the multi-day catalyst. On Fridays it uses a 6-hour lookback and focuses on weekend risk and Monday events (earnings, Fed, macro data). Positions flagged HOLD get fresh GTC SL+TP orders placed at current ATR levels before the market closes.

---

## Telegram Bot (optional)

Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env`. Create a bot via [@BotFather](https://t.me/BotFather).

| Command | Action |
|---|---|
| `/status` | Current positions, P&L, SL/TP |
| `/logs` | Last 20 lines of activity log |
| `/orders` | Session trades |
| `/say <msg>` | Send instruction to bot (e.g. "skip NVDA today") |
| `/run` | Start the bot service (launchd) |
| `/stop` | Stop the bot service |

Push notifications fire automatically on: BUY fill, SELL, TRIM, stop-out, risk halt.

---

## Dashboard

`dashboard.py` is a GUI control panel built with Tkinter. Run it in a separate terminal while the bot is running:

```bash
python dashboard.py
```

It lets you:
- **Start / stop** the bot service (launchd)
- **View live activity log** — tails `bot_activity.log` in real time
- **View open positions** — reads `session_trades.json` and shows qty, entry price, SL, TP
- **Send instructions** — write to `bot_instructions.txt` so the bot picks them up next cycle

Does not import any trading code — purely reads log and JSON files. Safe to open and close at any time without affecting trading.

---

## Logs

The bot writes to two log files:

**`bot_activity.log`** — main rotating log. Every cycle is recorded: market status, Grok scan results, Claude scores, orders placed, SL/TP updates, P&L. Rotates automatically at 2MB (keeps 3 backups).

**`orders.log`** — one entry per trade with fill price, SL, TP, R:R ratio, and the AI reasoning that triggered the buy.

### Recommended: clear logs daily

Logs grow fast and are most useful for diagnosing the current day. Clear them each morning before starting the bot:

```bash
> bot_activity.log && > orders.log
```

Keep logs when you need to debug something — they contain the full Grok news scan output and Claude's reasoning for every decision, which makes it easy to trace why a specific trade was placed or why a stop was tightened.

---

## Broker Support

Currently only **Interactive Brokers** is supported via the [ib_async](https://github.com/erdewit/ib_insync) library.

Support for other brokers (Alpaca, Tradier, TD Ameritrade) is not implemented but the architecture is modular — `account.py`, `trading.py`, and the connection logic in `main.py` are the only broker-specific files.

---

## Shariah Compliance

This bot was built for a Shariah-compliant portfolio:
- **No short selling** — only long positions
- **No margin** — cash account only
- **Screened watchlist** — only halal tech stocks (no banks, insurance, weapons, alcohol, tobacco, gambling)
- T+1 settlement respected — only settled cash is used for new buys

---

## Disclaimer

This is not financial advice. Automated trading involves significant risk of loss. Use paper trading mode to test before going live. The authors are not responsible for any financial losses.
