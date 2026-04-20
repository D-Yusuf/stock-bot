# Stock Bot — Architecture

## File Structure

```
~/stock-bot/
  main.py           Orchestrator — connects IBKR, starts Telegram, runs the trading loop
  config.py         AppContext dataclass, load_config(), env vars (XAI, IBKR, Telegram)
  logger.py         Logging setup, log(), log_order(), sanitize(), event bus
  helpers.py        is_market_open(), within_30min_of_open(), keyboard listener, Kuwait time
  account.py        get_total_capital(), get_available_cash() (settled cash / T+1)
  atr.py            ATR(14) calculator using IBKR historical bars
  risk.py           RiskGuard class — session trades, daily loss limit, cooldowns
  strategy.py       Two-pass Grok strategy: Pass A (news scan), Pass B (conviction scoring)
  trading.py        trade_rebalance(), close_all_positions(), check_for_stopouts()
  telegram_bot.py   Telegram bot — /run, /stop, /status, /logs, /orders, /say + push alerts
  dashboard.py      Tkinter GUI — Run/Stop/Check buttons, live log tail, session trades table
  config.json       Watchlist, risk params, ATR params, timing, Grok config
  .env              API keys: XAI_API_KEY, IBKR_HOST/PORT/ACCOUNT, TELEGRAM_BOT_TOKEN/CHAT_ID
```

## Data Flow

```
main.py
  ├── creates AppContext (ib, client, ib_acc)
  ├── creates RiskGuard (tracks session trades, daily P&L, cooldowns)
  ├── starts TelegramBot (shares ctx + risk references)
  │
  └── LOOP (every 5 min during market hours):
      ├── load_config()          → fresh config each cycle
      ├── check_for_stopouts()   → detect positions closed by SL/TP
      ├── get_grok_strategy()    → Pass A: X/web search → Pass B: scoring → allocation
      └── trade_rebalance()      → execute buys, manage SL/TP, trail stops
```

## Shared State

- **AppContext** — passed to every function that needs IBKR or Grok access
- **RiskGuard** — single instance, shared between main loop and Telegram bot
  - reset() method used on day rollover (preserves object reference)
- **Event Bus** — logger.py has emit_event()/register_event_listener()
  - trading.py emits: BUY, SELL, TRIM, STOPOUT, HALT
  - telegram_bot.py listens and pushes to phone

## Telegram Commands

| Command | What it does |
|---|---|
| /run | launchctl load (start bot service) |
| /stop | launchctl unload (stop bot service) |
| /status | Show current positions, session trades, P&L |
| /logs | Last 20 lines of bot_activity.log |
| /orders | Session trades from session_trades.json |
| /say msg | Save to bot_instructions.txt + ask Grok directly, reply in chat |
| /help | List commands |

## Key Design Decisions

1. **No globals** — AppContext passed explicitly, making every module testable
2. **Event bus** — loose coupling between trading logic and notifications
3. **risk.reset()** — avoids creating new RiskGuard on day rollover, keeps Telegram reference valid
4. **Telegram in same process** — shares asyncio loop with IBKR, no threading conflicts
5. **Config reloaded each cycle** — edit config.json while bot runs, changes take effect next cycle
