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

## BUG — Stale session_trades after restart showing closed positions
**Date:** 2026-04-21
**Severity:** Medium — bot holds phantom positions, attaches SL/TP to nothing
**Status:** Fixed 2026-04-21

### What happened
After restart, `session_trades` reloaded from disk with GOOGL=5sh, NVDA=8sh. But those
positions had already been trimmed/closed in IBKR. Bot logged "keeping 5sh" for GOOGL
and tried to attach SL+TP to NVDA which had 0 shares held.

### Fix applied
`main.py`: On startup, cross-check `session_trades` against actual IBKR positions.
Any ticker in session_trades with 0 shares in IBKR gets removed immediately.

---

## BUG — Trim logic uses session_trades qty vs IBKR actual qty causing short sell attempts
**Date:** 2026-04-21
**Severity:** Critical — causes repeated bad sells, Error 201 short sell rejections
**Status:** Needs fix

### What happened
Trim delta = `target_qty - session_qty`. But `session_qty` comes from session_trades
(stale, from before restart) while `current_qty` comes from IBKR (real). When
session_trades says 5sh but IBKR holds 1.5sh, bot tries to sell 3sh it doesn't own
→ Error 201 short sell rejection. ADBE was fully sold at a loss this way.

### Root cause
Trim should use `current_qty` (what IBKR actually holds) not `session_qty` (what
bot remembers buying). They diverge after any restart or partial fill.

### Fix needed
In trim calculation: `trim_qty = min(abs(delta_qty), current_qty)` not `session_qty`
Also: before any SELL, verify `current_qty > 0` to prevent short sell attempts.

### Status: Fixed 2026-04-21

---

## BUG — Rebalance trims session positions against shrinking free-cash budget
**Date:** 2026-04-21
**Severity:** Critical — sold MSFT (-$14.51) and ADBE (-$2.19) that should have been held
**Status:** Fixed 2026-04-21

### What happened
MSFT was bought at 1sh ($425). Later in the same session, the bot bought NVDA, TSM, ADBE,
AMD — consuming most of the $9k cap. Free budget dropped to $857. Next cycle, Grok gave
MSFT 30% allocation = $257 → at $425/sh = 0 shares. Delta = `0 - 1 = -1` → bot trimmed it.
Same happened to ADBE (2sh → 1sh trimmed).

### Root cause
`target_qty` was computed from *remaining free cash*, not the original full budget at time
of purchase. As more tickers were bought, the free budget shrank, making earlier positions
look "oversized" relative to the leftover cash — triggering spurious trims every cycle.

### Fix applied
`trading.py`: `target_qty = max(target_qty, session_qty)` when ticker is in session_trades.
Once a position is bought, it can only grow or hold via rebalance — never shrink due to
budget math. SL/TP and EOD close handle all legitimate exits.

---

## IMPROVEMENT IDEAS (backlog)

- [ ] Time expectancy per trade — Grok estimates hold window (hours/days)
- [ ] Dynamic hold updates — Grok re-evaluates held positions each cycle, can extend/shorten/exit
- [ ] Break-even stop — move SL to entry once position is up 1.5× ATR
- [ ] Session history logging — CSV + JSON per session for performance tracking
- [ ] Daily Telegram summary at market close
- [ ] Explicit exit signal required to trim — Grok must say "exit" not just "0 allocation"
