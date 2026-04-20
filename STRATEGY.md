# Trading Strategy

## Philosophy

News-driven momentum trading on a focused watchlist of 8 high-liquidity tech stocks.
The bot reads live news and social sentiment every 5 minutes, scores conviction, and
allocates capital only when signals are strong. Shariah compliance is non-negotiable.

**Core rules:**
- Long-only. No shorting, ever.
- No interest-bearing instruments, no banks, no weapons, no gambling, no alcohol.
- Prefer to hold a losing trade an extra day/week rather than close at a loss.
- Cut losses only as a last resort (hard SL is catastrophic protection, not routine exit).

---

## Watchlist

8 stocks chosen for liquidity, news sensitivity, and tech/AI relevance:

| Ticker | Company         | Why it's here |
|--------|-----------------|---------------|
| NVDA   | NVIDIA          | AI chip leader — reacts strongly to AI news |
| GOOGL  | Alphabet        | AI + cloud — moves on Gemini/search news |
| AAPL   | Apple           | High liquidity, moves on product/macro news |
| MSFT   | Microsoft       | Azure + Copilot — AI tailwind stock |
| AMD    | AMD             | NVIDIA competitor — chips/data center news |
| TSM    | TSMC            | Semiconductor supply chain — geopolitical sensitivity |
| CRM    | Salesforce      | Enterprise AI (Agentforce) — earnings sensitive |
| ADBE   | Adobe           | AI creative tools — Firefly/partnership news |

**To add/remove tickers:** Edit `config.json` → `watchlist` array.
Also add/remove the matching entry in `strategy.py` → `TICKERS_CONTEXT` dict.

---

## Trade Horizon

- **Primary:** Daily trades — open and close within the same day or hold 1-2 days max.
- **Secondary:** Weekly swing — if a multi-day catalyst exists (earnings, product launch),
  hold up to 5 trading days.
- **Zero-loss rule:** If a position is in the red at end of day, hold it into the next
  session rather than close at a loss. Exit only when back to break-even or better,
  unless the hard SL is hit (catastrophic move) or Grok flags a bearish reversal.

---

## Entry Rules

### Signal scoring (Grok Pass B, scored 1–10):

| Score | Signal quality | Action |
|-------|---------------|--------|
| 9–10  | Hard news (CNBC/Reuters/SEC) + BULLISH + HIGH volume | Buy, large allocation |
| 7–8   | Hard news alone OR BULLISH HIGH + verified accounts | Buy, normal allocation |
| 5–6   | BULLISH MEDIUM, no hard news | Skip (below threshold) |
| ≤ 4   | Weak, stale, or unverified | Skip |
| 0     | BEARISH or NODATA | Skip — or exit existing position |

**Minimum conviction to buy:** 7 (set in `config.json` → `grok.min_conviction_score`)

### Priority signal types (in order):
1. Unusual options flow or short squeeze chatter
2. Insider buy or large block trade
3. Earnings surprise or guidance update
4. Analyst upgrade from verified account
5. Major product launch or partnership
6. AI demand signal (chips, cloud)
7. Breaking CNBC/Reuters/SEC news

**To tune what Grok looks for:** Edit `config.json` → `grok.focus_topics` and `grok.ignore_topics`.

---

## Exit Rules

### Take Profit (TP)
- Set at entry: `fill_price + (ATR × tp_multiplier)`
- Default TP multiplier: **2.5x ATR**
- This is a limit sell order, GTC, part of an OCA group with the SL.

### Stop Loss (SL)
- Set at entry: `fill_price - (ATR × sl_multiplier)`
- Default SL multiplier: **1.5x ATR**
- This is a hard stop — only triggers on a catastrophic move.
- The SL trails upward as price rises (never moves down).
- **Zero-loss philosophy:** The SL is set wide intentionally so normal daily
  volatility doesn't trigger it. It's a last-resort safety net, not a routine exit.

### Risk:Reward ratio
- Default: 1 : 1.67 (1.5 SL vs 2.5 TP)
- To change: Edit `config.json` → `atr.sl_multiplier` and `atr.tp_multiplier`

### Early exit (future: Grok-driven)
- Each cycle, Grok re-evaluates held positions.
- If sentiment turns bearish on a held stock → bot can flag for early exit.
- If news confirms the original thesis → hold window extended.

---

## Risk Management

### Daily loss limit
- Bot halts all trading if account loses **3%** in a single day.
- Resets at midnight (new trading day).
- To change: `config.json` → `risk.daily_loss_limit_pct`

### Portfolio cap
- Maximum capital deployed: **$8,000**
- Protects against over-deployment in high-conviction multi-signal days.
- To change: `config.json` → `risk.portfolio_cap_usd`

### Cash buffer
- Always keep **2%** of available cash uninvested as a liquidity reserve.
- To change: `config.json` → `risk.cash_buffer_pct`

### Max single position
- No single stock can exceed **30%** of the investable budget.
- To change: `config.json` → `risk.max_single_position_pct`

### Cooldown after stop-out
- If a position is stopped out, that ticker is blocked for **3 cycles** (~15 min).
- Prevents re-entering a falling knife immediately.
- To change: `config.json` → `risk.cooldown_cycles_after_stopout`

### Shariah compliance
- Enforced in the Grok prompt — the model is instructed to skip any non-compliant stock.
- The watchlist itself is pre-screened (no banks, no weapons, no gambling, no alcohol).
- To update compliance rules: Edit `strategy.py` → `analysis_prompt` preamble.

---

## ATR Settings

ATR(14) is used to set SL/TP distances dynamically based on each stock's volatility.

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `period` | 14 | 14-bar ATR lookback |
| `sl_multiplier` | 1.5 | SL = price − (1.5 × ATR) |
| `tp_multiplier` | 2.5 | TP = price + (2.5 × ATR) |
| `trail_multiplier` | 1.0 | Trailing stop tightens to 1.0 × ATR |
| `sl_update_min_pct` | 0.5% | Only move trailing stop if improvement > 0.5% |

**To widen stops** (hold through more volatility): increase `sl_multiplier` to 2.0–2.5.
**To tighten TP** (take profit faster): decrease `tp_multiplier` to 1.5–2.0.

---

## Timing

- **Cycle:** Every 5 minutes during market hours.
- **Skip first 30 min:** No trades in the first 30 minutes after open (high volatility).
- **News lookback:** Grok looks back **1 hour** for signals, prioritises last 30 minutes.
- **After hours:** Bot stays alive, countdown timer shows time until next open.

---

## Planned Features

- [ ] **Time expectancy per trade** — Grok estimates how long a signal should last (hours/days).
      Bot holds the position for that window before considering exit.
- [ ] **Dynamic hold updates** — Each cycle, Grok re-evaluates held positions and can
      extend, shorten, or immediately exit based on updated news.
- [ ] **Break-even stop** — Once a position is up 1.5× ATR, move SL to entry price.
