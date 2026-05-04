# Stock Bot — Project Notes for Claude

## Account
- Broker: Interactive Brokers (IBKR)
- Account type: **Cash account** (NOT margin). No shorting, no leverage.
- Balance: ~$9,000 USD
- PDT rule does NOT apply to cash accounts — no day-trade count limit.
- Real constraint: T+1 settlement. Proceeds from a sale are available next business day.
  Cannot reuse same-day sale proceeds to open new positions (IBKR enforces this automatically).

## Trading Rules
- **Shariah-compliant only.** Never trade stocks that involve: alcohol, tobacco, pork/meat processing,
  conventional banking/interest-based financial services, gambling, adult entertainment, weapons/defense,
  or companies with excessive interest-bearing debt (debt-to-asset ratio > ~33%).
- **No short selling** under any circumstances — this is both a cash account restriction AND haram (forbidden in Islamic finance). Never place SELL orders that could result in a short position.
- **No margin trading.**
- **All SELL orders must be paired with a BUY** — the bot only sells what it already owns (take-profit or stop-loss on existing long positions). Never place a standalone SELL LMT or SELL STP without a corresponding owned position. Always use OCA groups to pair SL+TP together.
- **Tech-focused** portfolio. Prefer large-cap US tech (NVDA, GOOGL, AAPL, MSFT, META, AMZN, AMD, etc.)
  but always verify Shariah compliance before adding a new ticker.
- **Medium risk profile:** max daily drawdown 3% of account. Bot halts trading for the day if hit.

## Risk Controls (enforced in code)
- Daily loss limit: 3% of account equity at session start (~$270 on $9k)
- Stop-loss per trade: ATR × 1.5 below fill price
- Take-profit per trade: ATR × 2.5 above fill price
- Cash buffer: ~5.4% of portfolio always kept in cash
- No trades within first 30 min of market open
- No trades outside regular NYSE hours (09:30–16:00 ET, Mon–Fri)

## Shariah-Screened Watchlist (currently active)
NVDA, GOOGL, AAPL, AMD, AMZN, META

Disabled (Shariah or performance): MSFT, ADBE, TSM

Stocks to avoid (fail Shariah screen):
- Any conventional bank or insurer (JPM, BAC, GS, V, MA etc.)
- Weapons/defense (LMT, RTX, BA etc.)
- Alcohol/tobacco/gambling (BUD, MO, MGM etc.)

## Architecture

### Core Bot Files
- **`main.py`** — Slim orchestrator (~80 lines). Connects to IBKR, initialises all modules,
  runs the 15-min trading cycle, handles EOD logic (close/hold decisions, refresh SL/TP,
  write daily summary). Entry point for the launchd service.

- **`config.py`** — Loads `.env` and `config.json`. Defines `AppContext` dataclass
  (holds `ib`, `client`, `ib_acc`). `load_config()` is called every cycle so config.json
  changes take effect without restart.

- **`logger.py`** — Rotating file logger (`bot_activity.log`), `log()`, `log_order()`,
  `log_trade_csv()` (appends to `progress/trades.csv`), `sanitize()` (strips API keys from
  logs). Also contains a simple event bus: `emit_event()` / `register_event_listener()` used
  by `telegram_bot.py` for push notifications.

- **`helpers.py`** — Small utilities: `is_market_open()`, `within_30min_of_open()`,
  keyboard listener (press Q to quit gracefully).

- **`account.py`** — `get_total_capital(ctx)` and `get_available_cash(ctx)` — thin wrappers
  around ib_async calls that return account values.

- **`atr.py`** — `get_atr(ctx, contract, cfg)` — fetches 5-min bars from IBKR and calculates
  14-period ATR for dynamic SL/TP sizing.

- **`risk.py`** — `RiskGuard` class. Tracks daily P&L, enforces daily loss limit, position
  size caps, and cooldown after stop-outs. Persists session trades in `session_trades.json`.
  Has a `reset(new_equity)` method so the object can be reused across days without breaking
  Telegram's reference.

- **`strategy.py`** — `get_grok_strategy()` — two-pass AI analysis:
  Pass A: Grok with `x_search`+`web_search` for live news on each ticker.
  Pass B: Claude reasons over Grok's findings and returns structured BUY/HOLD/SKIP JSON
  with conviction scores, position sizes, SL/TP.
  `get_hold_overnight_flags()` — separate EOD call that decides which held positions to keep
  overnight. Friday mode uses 6h lookback and next-week catalyst analysis.

- **`trading.py`** — Core order management:
  `trade_rebalance()` — opens new positions based on strategy output.
  `close_all_positions()` — closes everything (EOD or manual), respects `hold_overnight` flags.
  `check_for_stopouts()` — polls open positions each cycle, exits if price hits SL threshold.
  Calls `emit_event()` on fills so Telegram gets push notifications.

- **`telegram_bot.py`** — Telegram bot for mobile control (python-telegram-bot library).
  Commands: `/status`, `/logs`, `/orders`, `/stop`, `/say <msg>`.
  Registers as event listener to receive BUY/SELL/STOPOUT push notifications automatically.

### Supporting Scripts
- **`dashboard.py`** — Separate process (not imported by main.py). Reads `bot_activity.log`
  and renders a live terminal dashboard. Run independently: `python dashboard.py`.

- **`check.py`** — Standalone IBKR connection test. Run before starting the bot to verify
  TWS/Gateway is reachable and the account is accessible.

- **`set_hold_overnight.py`** — Manual override script. Edit and run to force
  `hold_overnight=true/false` on specific tickers in `session_trades.json` without
  touching main.py.

### Config & Data Files
- **`config.json`** — All tunable parameters: watchlist, risk limits, ATR multipliers,
  Grok settings (model, conviction threshold, focus topics), EOD settings, cycle timing.
  Reloaded every cycle — edit freely without restarting.

- **`.env`** — Secret keys: `IBKR_ACCOUNT`, `XAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Never commit this file.

- **`session_trades.json`** — Live session state. Tracks open positions, entry prices,
  SL/TP levels, and `hold_overnight` flags for the current trading day. Auto-managed by
  `risk.py`. Reset each new trading day.

- **`bot_instructions.txt`** — Free-text overrides loaded by the bot each cycle. Write
  anything here (e.g. "skip NVDA today", "only buy if conviction > 8") and the bot passes
  it to Claude on the next cycle. Cleared automatically after use.

- **`orders.log`** — Plain-text record of every order placed (fills, rejections). Appended
  by `trading.py`. Useful for reconciling with IBKR trade history.

- **`bot_activity.log`** — Main rotating log (3 × 2MB). Human-readable record of every
  cycle: market check, Grok scan results, Claude decisions, orders, P&L. Rotates
  automatically via RotatingFileHandler.

### Docs
- **`STRUCTURE.md`** — Architecture diagram and module dependency graph.
- **`BUGS_AND_IMPROVEMENTS.md`** — Running log of bugs found/fixed and improvement ideas.
  Update this whenever a bug is found, even minor ones.
- **`STRATEGY.md`** — Trading strategy overview: how Grok+Claude hybrid works, conviction
  scoring, position sizing rationale.

### Directories
- **`progress/`** — Daily trading journal. One `YYYY-MM-DD.md` per trading day (written
  automatically by `_write_daily_summary()` at EOD). `trades.csv` is the master trade log
  with columns: `date, ticker, entry_price, exit_price, qty, pnl, exit_reason, hold_minutes`.

- **`venv/`** — Python virtual environment. Built with pyenv Python 3.12 (required on
  macOS Tahoe 26.x due to broken system libexpat). Never commit.

## Daily Progress Tracking
- **ALWAYS** write end-of-session summary to `progress/YYYY-MM-DD.md` without being asked
- **ALWAYS** update `progress/trades.csv` for any closed trades without being asked
- Keep progress files SHORT — no essays. Format:

```
# 2026-04-22
Net P&L: +$12.50 unrealized | $0 realized
Held overnight: GOOGL, MSFT, AMD, NVDA
Closed: ADBE -$17.81, CRM -$3.43
Bugs fixed: trim bug, JSON truncation
Features: hold_overnight, QQQ check, hybrid Grok+Claude
```

## Bug Tracking
- Any bug found (even minor) must be documented in `BUGS_AND_IMPROVEMENTS.md`
- Include: date, what happened, root cause, fix applied
- Improvement ideas and backlog items go in the same file under the improvements section

## Future Ideas
- SSH into Windows/Linux PC to run the bot there for more compute and uptime.
