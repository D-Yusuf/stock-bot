# Stock Bot — Full Application Overview

A Shariah-compliant, AI-driven algorithmic trading bot running on macOS. It connects to Interactive Brokers (IBKR) via TWS, scans live news using xAI Grok, makes buy/hold/sell decisions with Claude, and can be controlled from an Android phone via Telegram.

---

## High-Level Flow

```
python main.py
    │
    ├── _ensure_tws_running()       — auto-launch Trader Workstation if not open
    ├── ib.connectAsync()           — connect to IBKR TWS API
    ├── RiskGuard(starting_equity)  — initialize daily loss tracking
    ├── TelegramBot.start()         — start Telegram push/commands on phone
    │
    └── LOOP every 15 minutes:
            │
            ├── [market closed?]    → print countdown, sleep 1s, repeat
            │
            ├── [EOD window? 10 min before 4 PM ET]
            │       ├── get_hold_overnight_flags()   — Grok + Claude decide hold/close per ticker
            │       ├── _refresh_overnight_sl_tp()   — place fresh SL/TP for overnight holds
            │       ├── close_all_positions()         — close everything not flagged hold
            │       ├── _write_daily_summary()        — Claude writes progress/YYYY-MM-DD.md
            │       └── risk.reset()                  — reset daily loss counter for next day
            │
            ├── [no free cash?]     → skip Grok scan, just manage existing SL/TP
            │
            ├── get_grok_strategy() — two-pass AI scan:
            │       ├── Pass A: Grok searches X + web for live news
            │       └── Pass B: Claude scores each ticker 1-10, outputs USD allocations
            │
            └── trade_rebalance()   — execute BUY/TRIM/manage SL orders on IBKR
```

---

## File Structure

```
~/stock-bot/
  main.py              # orchestrator — loop, EOD, TWS auto-launch
  config.py            # env vars, AppContext, load_config()
  logger.py            # logging, CSV trade log, event bus
  helpers.py           # market hours checks, keyboard listener
  account.py           # IBKR account value queries
  atr.py               # ATR calculation for SL/TP sizing
  risk.py              # RiskGuard — daily loss limit, session trades
  strategy.py          # Grok+Claude AI strategy (Pass A + Pass B)
  trading.py           # order execution (buy, sell, trim, stopout)
  telegram_bot.py      # Telegram bot for mobile control
  dashboard.py         # standalone terminal dashboard (separate process)
  check.py             # standalone IBKR connection test
  set_hold_overnight.py # manual script to set hold_overnight flags
  config.json          # all tunable parameters (reloaded every cycle)
  .env                 # API keys and account IDs (never commit)
  session_trades.json  # live session state (auto-managed)
  bot_instructions.txt # free-text overrides picked up each cycle
  orders.log           # plain-text fill log
  bot_activity.log     # rotating activity log (3 × 2MB)
  progress/
    trades.csv          # master closed-trade log
    YYYY-MM-DD.md       # daily summaries (written automatically at EOD)
```

---

## Module-by-Module Breakdown

---

### `main.py` — Orchestrator

**What it does:**  
Entry point. Launches TWS if needed, connects to IBKR, initializes all modules, then runs the main trading loop indefinitely.

**Key functions:**

`_ensure_tws_running()`
- Checks if the IBKR API port is open (socket probe to 127.0.0.1:7496)
- If TWS is not running: launches it with `open -a "Trader Workstation.app"`
- Polls the port every 3s for up to 120s waiting for it to open
- Raises a clear error if the port never opens (with instructions to check API settings in TWS)

`_write_daily_summary(ctx, risk, cfg, held_overnight)`
- Called once at EOD after positions are closed
- Reads the last 60 lines of `bot_activity.log` and current `session_trades`
- Sends it all to Claude with a structured prompt
- Claude writes a short `progress/YYYY-MM-DD.md` file and returns any new CSV rows to append to `trades.csv`
- Both files are written automatically — no manual action needed

`_refresh_overnight_sl_tp(ctx, risk, cfg, flags)`
- Called at EOD for positions flagged `hold_overnight=True`
- Cancels the existing SL+TP orders for those positions
- Recalculates SL/TP based on current price + fresh ATR
- Places new GTC SL+TP orders so they're valid overnight
- Updates `session_trades.json` with the new SL/TP values

**Main loop logic (inside `main()`):**
1. Reload `config.json` every cycle (changes take effect without restart)
2. Reconnect to IBKR if connection dropped silently
3. Reset `RiskGuard` if it's a new trading day
4. Tick down cooldown counters; detect position stop-outs
5. If market is closed → print countdown, sleep 1s per tick
6. If in EOD window → run hold decision, close/keep positions, write summary, reset
7. If no free cash → skip Grok scan entirely (saves API cost), just manage SL/TP
8. Otherwise → run `get_grok_strategy()` → `trade_rebalance()`
9. Smart sleep: if EOD window is approaching within the next cycle, wake up early to catch it

**Shutdown:**  
- Press `x + Enter` → closes all positions then exits cleanly
- `Ctrl-C` → same
- Both paths call `close_all_positions()` and disconnect from IBKR

---

### `config.py` — Configuration and App Context

**What it does:**  
Loads environment variables from `.env`, reads `config.json` to determine live vs paper mode, and provides `AppContext` — the single object passed everywhere that holds the three clients.

**Key exports:**
- `IB_HOST`, `IB_PORT`, `IB_ACC`, `TRADING_MODE` — resolved from `.env` + `config.json`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — from `.env`
- `BOT_DIR` — absolute path to the `~/stock-bot/` directory
- `AppContext` — dataclass with `ib` (ib_async IB), `client` (xAI/Grok via OpenAI SDK), `claude` (Anthropic), `ib_acc` (account string)
- `create_context()` — instantiates `AppContext` with live clients
- `load_config()` — reads and returns `config.json` as a dict; called every cycle

**Live vs paper mode:**  
`config.json → "mode": "live"` or `"paper"`. Live uses port 7496 and `IBKR_ACCOUNT_LIVE`; paper uses 7497 and `IBKR_ACCOUNT_PAPER`.

---

### `logger.py` — Logging, CSV, Event Bus

**What it does:**  
Sets up all output: rotating log file, order log, trade CSV, and a simple publish/subscribe event bus for Telegram push notifications.

**Rotating log (`bot_activity.log`):**
- `log(msg, level="info")` — prints to console AND writes to rotating log file
- 3 backups of 2MB each; old backups auto-deleted
- Format: `2026-05-01 14:32:11 | INFO | message`

**Order log (`orders.log`):**
- `log_order(action, ticker, qty, price, sl, tp, reason)` — writes a formatted human-readable block per trade with SL, TP, R:R ratio, and analysis bullets
- Appended forever, never overwritten

**Trade CSV (`progress/trades.csv`):**
- `log_trade_csv(ticker, entry_price, exit_price, qty, exit_reason, entry_time)` — appends one row per closed trade
- Columns: `date, ticker, entry_price, exit_price, qty, pnl, exit_reason, hold_minutes`
- P&L is `(exit - entry) × qty` — IBKR already deducts commissions from P&L so we don't subtract again

**Event bus:**
- `emit_event(event_type, data)` — called by `trading.py` when a fill happens
- `register_event_listener(fn)` — `TelegramBot` registers here at startup
- Events: `BUY`, `SELL`, `TRIM`, `STOPOUT`, `HALT`
- `sanitize(text)` — strips account IDs and dollar amounts from text before sending to external APIs

---

### `helpers.py` — Market Hours and Utilities

**What it does:**  
Time-based checks and the keyboard shutdown listener. Everything here is stateless.

**Functions:**
- `is_market_open()` → `True` between 09:30–16:00 ET, Mon–Fri
- `within_30min_of_open()` → `True` 09:30–09:40 ET (skip first 10 min to avoid open volatility)
- `is_eod_close_window(minutes_before)` → `True` when within N minutes of 16:00 ET close
- `is_friday_eod(minutes_before)` → same but only on Fridays (weekend close)
- `next_market_open_secs()` → seconds until next 09:30 ET open (skips weekends)
- `kuwait_time_str()` → current time in Asia/Kuwait timezone (for the countdown display)
- `start_keyboard_listener()` → spawns a daemon thread; sets `shutdown_requested = True` when user types `x + Enter`

---

### `account.py` — IBKR Account Queries

**What it does:**  
Two thin wrappers that read account values already cached by ib_async.

- `get_total_capital(ctx)` → reads `NetLiquidation` tag from IBKR account values. This is the total portfolio value (cash + market value of all positions).
- `get_available_cash(ctx)` → reads `SettledCash` (preferred) or falls back to `TotalCashValue`. **Critical for cash accounts**: T+1 settlement means proceeds from selling today aren't spendable until tomorrow. `SettledCash` reflects this correctly. If settled ≠ total, it logs the difference.

---

### `atr.py` — Average True Range

**What it does:**  
Calculates 14-period ATR from daily bars. Used to size SL and TP dynamically based on recent volatility rather than fixed percentages.

**How it works:**
1. Requests `period + 5` daily bars from IBKR for the given contract
2. Calculates True Range for each bar: `max(high-low, |high-prev_close|, |low-prev_close|)`
3. Returns the simple average of the last 14 TRs

**Usage in trading:**
- `SL = entry_price - (ATR × sl_multiplier)` (default 1.5)
- `TP = entry_price + (ATR × tp_multiplier)` (default 2.5)
- Default R:R ratio: 1:1.67 (TP is 2.5× as far as SL)

These multipliers are tunable in `config.json → atr`.

---

### `risk.py` — RiskGuard

**What it does:**  
Tracks daily P&L, enforces loss limits, manages per-ticker cooldowns after stop-outs, and persists open position state across bot restarts in `session_trades.json`.

**RiskGuard class:**

`__init__(starting_equity, daily_loss_pct, cooldown_cycles)`
- Computes `loss_limit = starting_equity × daily_loss_pct`
- Loads `session_trades.json` from disk (discards if from a previous day)
- Restores in-memory session trade records so the bot picks up where it left off after a restart

`reset(new_equity)`
- Called at start of new trading day
- Resets halt state, clears all session trades and cooldowns, saves empty `session_trades.json`
- Designed to be called on the same object so Telegram's reference stays valid

`check_halt(current_equity) → (bool, reason)`
- Returns `(False, reason)` if daily loss limit has been hit
- Otherwise returns `(True, "")` and logs how much loss budget remains

`record_session_trade(ticker, qty, fill_price, sl, tp)`
- Stores trade in memory and persists to `session_trades.json`
- Tracks `cash_deployed` total so free budget can be calculated accurately

`close_session_trade(ticker)` — removes from memory and disk (called on fill or stop-out)

`enter_cooldown(ticker)` / `in_cooldown(ticker)` / `tick_cooldowns()`
- After a stop-out, a ticker is put in cooldown for N cycles (default 3 = ~45 min)
- `tick_cooldowns()` is called each cycle to count down and free tickers

**`session_trades.json` format:**
```json
{
  "date": "2026-05-01",
  "trades": {
    "NVDA": {
      "qty": 5,
      "fill_price": 199.76,
      "sl": 190.18,
      "tp": 215.72,
      "time": "10:15:30",
      "entry_dt": "2026-05-01T10:15:30",
      "hold_overnight": false,
      "last_exit_price": null
    }
  }
}
```

---

### `strategy.py` — Grok + Claude AI Strategy

**What it does:**  
Two separate AI functions: `get_grok_strategy()` for intraday buy decisions and `get_hold_overnight_flags()` for EOD hold/close decisions. Both use a two-pass architecture: Grok searches live data, Claude reasons.

---

**`get_grok_strategy(ctx, watchlist, conversation, cfg, investable)`**

The main strategy function. Returns `{ticker: pct_of_budget}` for all qualifying buy signals, or `None` if nothing passes the threshold.

**Pass A — Grok live search:**
- Calls `ctx.client.responses.create()` (xAI API, OpenAI-compatible SDK) with tools `x_search` and `web_search`
- Searches X (Twitter) by cashtag (e.g. `$NVDA`) and whitelisted news sites (Reuters, Bloomberg, CNBC, SEC, Stocktwits)
- Prompt asks for: QQQ % change today, then for each ticker: BULLISH/BEARISH/NEUTRAL, HIGH/MEDIUM/LOW confidence, top signal, any hard news
- Output is raw text like: `NVDA | BULLISH | HIGH | Jensen Huang confirmed Blackwell demand up 3x | CNBC reported 10:45 AM`

**Pass B — Claude reasoning:**
- Sends Pass A output to `ctx.claude.messages.create()` (Anthropic API)
- Claude scores each ticker 1–10 using a rubric:
  - 9-10: hard news (CNBC/Reuters/SEC) + BULLISH + HIGH volume
  - 7-8: hard news alone OR BULLISH HIGH from verified accounts
  - 5-6: BULLISH MEDIUM, no hard news
  - ≤4 or BEARISH or NODATA: skip
- Claude outputs JSON: `{"conviction": {"NVDA": 8}, "reasoning": ["NVDA (8/10): ..."], "usd_allocation": {"NVDA": 2500}}`
- USD allocations are converted to % of `investable` budget, capped by `max_single_position_pct` and `allocation_caps` in config

**QQQ guard:** If QQQ is down >1.5% on the day, Claude sets all allocations to 0 (no buys in broad market selloff).

**No-cash mode:** If `investable = 0`, the prompt switches to analysis-only mode — Claude scores normally but sets all USD allocations to 0, focusing only on whether existing positions should exit early.

**`TICKERS_CONTEXT` dict** maps each ticker to its X cashtag and search keywords. To add a new stock: add it here AND in `config.json → watchlist`.

---

**`get_hold_overnight_flags(ctx, current_positions, cfg, is_friday=False)`**

Called once at EOD. Returns `{ticker: bool}` — True means keep the position overnight, False means close it.

**Pass A — Grok EOD/weekend scan:**
- Normal day: 4h lookback, asks about next 1–5 day catalysts
- Friday: 6h lookback, explicitly asks about weekend risk events, Monday earnings, analyst days, macro data (Fed, CPI, jobs), and gap-down risk
- Returns one line per ticker: `GOOGL | BULLISH | HIGH | AWS beat confirmed, analyst targets raised | No major Monday events`

**Pass B — Claude hold/close decision:**
- Sees position data (qty, entry, current price, P&L) + Pass A news
- Normal day criteria: hold if strong multi-day catalyst; close if intraday signal, stale news, or bearish
- Friday criteria: much more selective — only hold if the setup is "genuinely strong" given 3 days of gap risk
- Returns JSON: `{"GOOGL": true, "NVDA": false}`

---

### `trading.py` — Order Execution

**What it does:**  
All IBKR order placement. Three public functions called by `main.py`.

---

**`close_all_positions(ctx, watchlist, reason, risk)`**

Closes every held position in the watchlist. Called at EOD, on shutdown, or Ctrl-C.

1. Cancels all open SL/TP orders for watchlist tickers
2. Skips any ticker where `session_trades[ticker]["hold_overnight"] == True` (when reason is "end of day" or "Friday close")
3. Places a DAY market sell order for each remaining ticker
4. Waits up to 30s for fill confirmation
5. On fill: calls `log_trade_csv()` with real fill price from IBKR

**Fractional shares protection:** Uses `sell_qty = int(qty)` — floors to whole shares because IBKR rejects fractional market sell orders (Error 10243).

---

**`check_for_stopouts(ctx, risk, prev_positions, watchlist)`**

Called every cycle. Detects positions that were closed by IBKR (i.e. SL or TP fired automatically).

- Compares current IBKR positions to `prev_positions` from the previous cycle
- If a ticker had shares before and now has 0 → it was stopped out
- Calls `log_trade_csv()` using `last_exit_price` (captured by the `execDetailsEvent` hook in `main.py`) for accurate P&L
- Calls `risk.enter_cooldown(ticker)` to prevent immediately re-buying a just-stopped position
- Emits `STOPOUT` event → Telegram push notification

---

**`trade_rebalance(ctx, strategy, risk, cfg, reasoning)`**

The main per-cycle trading function. `strategy` is `{ticker: pct_of_budget}` from Grok/Claude.

For each ticker in the watchlist (both strategy targets and currently held):

**Pre-existing positions** (held before this bot session, e.g. carried overnight):
- Checks if SL/TP orders exist; places them if missing
- Manages trailing stop: if price has moved up, raises the stop to lock in gains

**Delta calculation:**
- `target_qty = (pct / 100) × investable / price`
- `delta = target_qty - session_qty`
- Never trims below the session-entry quantity (avoids spurious trims as budget shrinks across buys)
- If Grok returns 0% for a held position, that means "no new buy signal" — the position is held by default (only SL/TP exits it)

**Case 1 — At or above target (delta ≤ 0):**
- If significantly over target (>10% excess): **TRIM** the excess shares
  - Cancels existing SL/TP, places market sell for delta qty, places new OCA SL+TP for remainder
- If at target: **trail the stop** — if price has moved up enough, replace the stop at a higher price

**Case 2 — Under target (delta ≥ 1 share, `MIN_DELTA`):**
- **BUY** delta qty via DAY market order
- Waits up to 30s for fill confirmation
- On fill: records in `risk.session_trades`, places OCA SL+TP pair
- OCA groups ensure only one of SL/TP fires; the other is auto-cancelled by IBKR when the first fills

**OCA (One-Cancels-All) order structure:**
Every position has exactly one SL (StopOrder SELL GTC) and one TP (LimitOrder SELL GTC) linked in an OCA group. When either fires, IBKR automatically cancels the other. This is how the bot protects every position 24/7 without polling.

---

### `telegram_bot.py` — Mobile Control

**What it does:**  
Runs a Telegram bot alongside the main trading loop. Lets you monitor and control the trading bot from your Android phone.

**Commands:**

| Command | What it does |
|---|---|
| `/help` | Lists all commands |
| `/run` | Runs `launchctl load ~/Library/LaunchAgents/com.stockbot.plist` to start the bot service |
| `/stop` | Runs `launchctl unload` to stop the bot service |
| `/status` | Fetches live IBKR positions + session trades with SL/TP and deployed cash |
| `/logs` | Returns last 20 lines of `bot_activity.log` (trimmed to Telegram's 4096 char limit) |
| `/orders` | Returns contents of `session_trades.json` formatted cleanly |
| `/say <msg>` | Writes the message to `bot_instructions.txt` (bot picks it up next cycle) AND asks Grok directly, returning the answer immediately |

**Push notifications (automatic):**  
On every `BUY`, `SELL`, `TRIM`, `STOPOUT`, and `HALT` event, the bot sends a message to your Telegram chat. These fire the moment the order fills — you don't need to check `/status`.

Example messages:
```
🟢 BUY 3x NVDA @ $199.76
SL: $190.18 | TP: $215.72

⛔ STOP-OUT MSFT — position gone, entering cooldown

🚨 TRADING HALTED — Daily loss limit hit: lost $278 (limit $270)
```

**Setup:**  
Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env`. Create a bot via @BotFather on Telegram, get your chat ID by messaging @userinfobot.

---

### `dashboard.py` — Terminal Dashboard

**What it does:**  
A separate, read-only process that tails `bot_activity.log` and renders a live terminal UI. Run it in a second terminal window while the bot is running.

```
python dashboard.py
```

Does not import anything from the trading bot — purely reads the log file. Safe to run at any time without affecting trading.

---

### `check.py` — Connection Test

**What it does:**  
Standalone script to verify that TWS is running, accepting connections, and that the `.env` account ID matches what IBKR reports.

```
python check.py
```

Run this before starting the bot for the first time, or after changing `.env` values. Connects with `clientId=10` (different from main bot's `clientId=15`) so there's no conflict.

---

### `set_hold_overnight.py` — Manual Hold Override

**What it does:**  
Manual override script. If you want to force a position to be held (or closed) at EOD without waiting for the bot's AI decision, run this:

```
python set_hold_overnight.py
```

It connects to IBKR, reads current positions, calls `get_hold_overnight_flags()` (same AI function the bot uses), and writes the results to `session_trades.json`. The main bot reads these flags at EOD and respects them.

Useful when: the bot is running but you want to pre-set the overnight decision, or the AI decision didn't run (e.g. EOD window was missed).

---

## Config Files

### `config.json`

All tunable parameters. Reloaded every 15-minute cycle — no restart needed.

```json
{
  "mode": "live",                    // "live" or "paper"
  "watchlist": ["NVDA", "GOOGL", "AAPL", "AMD", "AMZN", "META"],
  "_watchlist_disabled": ["MSFT", "ADBE", "TSM"],

  "risk": {
    "daily_loss_limit_pct": 0.03,    // halt trading if daily loss exceeds 3%
    "portfolio_cap_usd": 9300,       // max total deployed (prevents IBKR margin)
    "cash_buffer_pct": 0.054,        // always keep ~5.4% as cash
    "max_single_position_pct": 0.30, // max 30% of budget in one stock
    "max_deploy_pct": 1.0,           // fraction of investable to actually use
    "cooldown_cycles_after_stopout": 3  // cycles to wait before re-buying a stopped ticker
  },

  "eod": {
    "close_all_eod": true,           // close positions at end of every day
    "close_all_friday": true,        // hard close on Friday
    "close_minutes_before": 10       // start EOD window 10 min before 4 PM ET
  },

  "atr": {
    "period": 14,
    "sl_multiplier": 1.5,            // SL = entry - ATR × 1.5
    "tp_multiplier": 2.5,            // TP = entry + ATR × 2.5
    "trail_multiplier": 1.0,
    "sl_update_min_pct": 0.005       // only trail stop if it would move >0.5%
  },

  "timing": {
    "cycle_seconds": 900,            // 15-minute cycles
    "skip_first_minutes": 30         // skip first 30 min after open
  },

  "claude": {
    "model": "claude-sonnet-4-6"
  },

  "grok": {
    "model": "grok-4",
    "min_conviction_score": 7,       // minimum score to place a buy
    "news_lookback_hours": 1,
    "focus_topics": [...],           // signals Grok should look for
    "ignore_topics": [...]           // stale/low-quality signals to ignore
  }
}
```

### `.env`

```
IBKR_ACCOUNT_LIVE=U1234567
IBKR_ACCOUNT_PAPER=DU1234567
IBKR_PORT_LIVE=7496
IBKR_PORT_PAPER=7497
IBKR_HOST=127.0.0.1
XAI_API_KEY=xai-...
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321
```

---

## Runtime Data Files

### `session_trades.json`

Auto-managed by `RiskGuard`. Persisted to disk after every buy/sell so the bot survives restarts. Discarded on new trading day.

Stores per-ticker: qty, fill price, SL, TP, hold_overnight flag, commission paid, entry datetime.

### `bot_instructions.txt`

Free-text file read by the bot at the start of every cycle. Write anything here — "skip NVDA today", "only buy if score > 8", "be conservative, market looks choppy". Claude sees it in the Pass B prompt. The `/say` Telegram command writes here automatically.

### `orders.log`

Appended every time a BUY, SELL, or TRIM fires. Human-readable format with R:R ratio and Grok's analysis bullet points for that ticker. Useful for post-session review.

---

## Dependencies

```
ib_async          — async IBKR TWS API client
openai            — used as xAI/Grok client (compatible API)
anthropic         — Claude API
python-dotenv     — load .env
python-telegram-bot — Telegram commands + push
```

Install:
```bash
pip install ib_async openai anthropic python-dotenv python-telegram-bot
```

**macOS Tahoe (26.x) note:** System Python and all Homebrew Pythons (3.12, 3.14) are broken due to a missing `libexpat` symbol (`_XML_SetAllocTrackerActivationThreshold`). Must use pyenv:
```bash
brew install pyenv
pyenv install 3.12.13
~/.pyenv/versions/3.12.13/bin/python -m venv venv
source venv/bin/activate
pip install ...
```

---

## Shariah Compliance Rules

Hard-coded constraints throughout:
- **No short selling** — only BUY orders open positions; SELL orders only close existing longs
- **No margin** — cash account, IBKR enforces this at the broker level
- **Screened watchlist only** — active: NVDA, GOOGL, AAPL, AMD, AMZN, META
- **Stocks never traded:** conventional banks, insurers, weapons/defense, alcohol, tobacco, gambling, adult entertainment
- **T+1 settlement** — only `SettledCash` used for new buys; prevents accidental use of unsettled funds

---

## launchd Service (auto-start on login)

The bot runs as a macOS background service so it starts automatically:

```
~/Library/LaunchAgents/com.stockbot.plist
```

Control it:
```bash
launchctl load   ~/Library/LaunchAgents/com.stockbot.plist   # start
launchctl unload ~/Library/LaunchAgents/com.stockbot.plist   # stop
```

Or from Telegram: `/run` and `/stop`.

---

## Daily Progress Files

Every trading day, Claude auto-writes `progress/YYYY-MM-DD.md` at EOD:

```
# 2026-05-01
Net P&L: +$694.82 realized | NVDA ~-$1 unrealized at close
Closed: GOOGL manual close +$626.63 | AMD manual close +$45.85 | AMZN manual close +$22.34
Held overnight: NVDA
Bugs: EOD window missed (Grok scan took 3 min, pushed past 4 PM)
Notes: AAPL +4.8% earnings, GOOGL cloud beat, AMZN AWS beat
API cost: ~$1.50 Grok | ~$0.30 Claude
```

Closed trades are appended to `progress/trades.csv`:

```
date,ticker,entry_price,exit_price,qty,pnl,exit_reason,hold_minutes
2026-05-01,GOOGL,346.02,385.78,17,+626.63,manual close,?
```
