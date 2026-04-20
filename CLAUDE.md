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
- Stop-loss per trade: 7% below fill price
- Take-profit per trade: 15% above fill price
- Cash buffer: 2% of portfolio always kept in cash
- No trades within first 30 min of market open
- No trades outside regular NYSE hours (09:30–16:00 ET, Mon–Fri)

## Shariah-Screened Watchlist (currently approved)
NVDA, GOOGL, AAPL, MSFT, AMD, TSM, CRM, ADBE

Stocks to avoid (fail Shariah screen):
- Any conventional bank or insurer (JPM, BAC, GS, V, MA etc.)
- Weapons/defense (LMT, RTX, BA etc.)
- Alcohol/tobacco/gambling (BUD, MO, MGM etc.)
- META — borderline; social media advertising is permissible but monitor for new business lines.

## Architecture
- `main.py` — main trading loop
- `bot_instructions.txt` — user overrides loaded each cycle (editable without restart)
- `check.py` — standalone IBKR connection test
- `.env` — API keys and account config (never commit)

## Bug Tracking
- Any bug found (even minor) must be documented in `BUGS_AND_IMPROVEMENTS.md`
- Include: date, what happened, root cause, fix applied
- Improvement ideas and backlog items go in the same file under the improvements section
- This helps us spot patterns (e.g. same bug recurring) and track what's been fixed

## Future Ideas
- SSH into Windows/Linux PC to run the bot there for more compute and uptime.
