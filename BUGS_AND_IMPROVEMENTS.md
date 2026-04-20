# Bugs & Improvements Log

---

## BUG — Trim loop selling all positions when Grok returns bearish
**Date:** 2026-04-20
**Severity:** Critical — caused major unintended sells
**Status:** Fixed 2026-04-21

### What happened
After a bot restart mid-session, `session_trades` reloaded from disk but `cash_deployed`
reset to 0. This made `investable` appear positive. Grok then returned 0% allocation for
held tickers (bearish signals on NVDA, AAPL, AMD etc). The rebalance loop saw
`target=0, held>0` and trimmed everything to 0 — repeatedly, every cycle.

### Root cause
Two separate issues compounding:
1. `cash_deployed` reset to 0 on restart → free budget looked larger than reality
2. Grok returning 0 allocation was treated as "sell" rather than "no opinion / hold"

### Fix applied
- `risk.py`: `cash_deployed` now starts at 0 on restart (positions already in IBKR,
  no need to re-lock that cash)
- `trading.py`: When Grok returns 0 allocation for a held position, **hold by default**.
  Only trim if Grok explicitly flags an exit via `position_updates` (future feature).
  A missing or zero allocation = "no new buy signal", not "sell".

---

## BUG — Trim loop (original) selling positions when investable=$0
**Date:** 2026-04-17
**Severity:** Critical — sold ~$6k of positions including TSM at a loss
**Status:** Fixed 2026-04-17

### What happened
When available cash was $0, `investable=0` → `target_qty=0` for all tickers →
`delta = 0 - session_qty` was negative → bot trimmed all session positions every cycle.

### Fix applied
`trading.py`: `if investable <= 0: target_qty = session_qty` — keeps target equal to
current holding so delta=0 and no trim is triggered.

---

## BUG — AAPL TP Error 201 "would create short position"
**Date:** 2026-04-17
**Severity:** Medium — TP orders rejected for pre-existing positions
**Status:** Fixed 2026-04-17

### What happened
Standalone SELL LMT orders (TP) placed without an OCA group were rejected by IBKR
with Error 201 — treated as potential short sells.

### Fix applied
`trading.py`: TP is now always placed together with SL in an OCA group. When pairing
protection for pre-existing positions, the existing standalone SL is cancelled and
replaced with a new OCA-paired SL+TP.

---

## IMPROVEMENT IDEAS (backlog)

- [ ] Time expectancy per trade — Grok estimates hold window (hours/days)
- [ ] Dynamic hold updates — Grok re-evaluates held positions each cycle, can extend/shorten/exit
- [ ] Break-even stop — move SL to entry once position is up 1.5× ATR
- [ ] Session history logging — CSV + JSON per session for performance tracking
- [ ] Daily Telegram summary at market close
- [ ] Explicit exit signal required to trim — Grok must say "exit" not just "0 allocation"
