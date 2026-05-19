import os
import re
import json
import datetime
from config import AppContext, BOT_DIR
from logger import log, sanitize

# ──────────────────────────────────────────────────────────────────────────────
# TICKERS_CONTEXT
# Maps ticker → (X cashtag, search keywords)
# Used in Pass A to build the X/web search query.
# ADD a new stock here AND in config.json → watchlist to start trading it.
# ──────────────────────────────────────────────────────────────────────────────
TICKERS_CONTEXT = {
    "NVDA":  ("$NVDA",  "NVIDIA Jensen Huang Blackwell"),
    "GOOGL": ("$GOOGL", "Google Alphabet Gemini"),
    "AAPL":  ("$AAPL",  "Apple iPhone Tim Cook"),
    "MSFT":  ("$MSFT",  "Microsoft Azure Copilot"),
    "AMD":   ("$AMD",   "AMD Lisa Su EPYC"),
    "AMZN":  ("$AMZN",  "Amazon AWS Bezos AI cloud"),
    "META":  ("$META",  "Meta Zuckerberg Facebook Instagram AI"),
}


async def get_hold_overnight_flags(
    ctx: AppContext,
    current_positions: dict,
    cfg: dict,
    is_friday: bool = False,
) -> dict:
    """
    EOD check — runs a Grok news scan per held ticker, then asks Claude
    hold/close based on live news + position data.
    On Fridays, does deeper multi-day research (next week catalysts).
    Returns {ticker: bool}. Called once near EOD, not every cycle.
    """
    if not current_positions:
        return {}

    gcfg    = cfg.get("grok", {})
    tickers = list(current_positions.keys())

    # ── Pass A: Grok news scan ────────────────────────────────────────────────
    search_targets = "\n".join(
        f"  {t}: X cashtag {TICKERS_CONTEXT[t][0]}, keywords: {TICKERS_CONTEXT[t][1]}"
        for t in tickers if t in TICKERS_CONTEXT
    )

    if is_friday:
        search_prompt = f"""Deep research for weekend hold decision. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}
These positions may be held over the ENTIRE WEEKEND (Fri close → Mon open).

{search_targets}

For each ticker report ONE line:
TICKER | BULLISH/BEARISH/NEUTRAL | HIGH/MEDIUM/LOW | next-week catalyst (1 sentence) | any Monday events: earnings, Fed, macro data, analyst days, product launches?

Focus on: next week earnings calendar, analyst price targets, macro events (Fed, CPI, jobs), institutional buying, weekend risk events.
Look back 6 hours. Flag any known weekend risk (geopolitical, regulatory, earnings surprise risk)."""
    else:
        search_prompt = f"""Research overnight risk/reward for these stock positions. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}

{search_targets}

For each ticker find: any major news event happening tonight or tomorrow (earnings, product launch, Fed decision, analyst day, FDA ruling, etc.) and the current market sentiment. Report what you find — be thorough, don't summarize away important details."""

    raw_news = ""
    try:
        search_response = ctx.client.responses.create(
            model=gcfg.get("model", "grok-4"),
            input=[{"role": "user", "content": sanitize(search_prompt)}],
            tools=[
                {"type": "x_search"},
                {
                    "type": "web_search",
                    "allowed_domains": ["reuters.com", "bloomberg.com", "cnbc.com", "sec.gov", "stocktwits.com"]
                },
            ],
        )
        raw_news = search_response.output_text
        log(f"{'FRIDAY DEEP' if is_friday else 'EOD'} NEWS SCAN:")
        for line in raw_news.strip().splitlines():
            log(f"  {line}")
    except Exception as e:
        log(f"EOD Grok scan error: {e} — Claude will decide without news context.", "warning")

    # ── Pass B: Claude decides hold/close ────────────────────────────────────
    positions_lines = "\n".join(
        f"  {t}: {info['qty']}sh @ ${info['entry']:.2f} | now ${info['price']:.2f} | P&L ${info['pnl']:+.2f}"
        for t, info in current_positions.items()
    )

    news_section = f"LIVE NEWS:\n{raw_news}" if raw_news else "No live news available — decide based on position data only."

    weekend_note = "These positions would be held over the ENTIRE WEEKEND (Friday close → Monday open)." if is_friday else ""

    prompt = f"""You are a trading analyst deciding which positions to hold overnight for a Shariah-compliant long-only portfolio. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}
{weekend_note}

HELD POSITIONS:
{positions_lines}

{news_section}

Hard rule: if a stock has earnings releasing TODAY after close or TOMORROW before open → always return true. Earnings are a known catalyst and the position must be held.

For everything else, use your judgment — weigh the overnight risk vs reward and make the smartest call for each position.

Return ONLY valid JSON, no markdown:
{{"GOOGL": true, "MSFT": false}}
"""

    try:
        claude_cfg = cfg.get("claude", {})
        model      = claude_cfg.get("model", "claude-sonnet-4-6")
        response   = ctx.claude.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text
        clean   = re.sub(r'```(?:json)?|```', '', content).strip()
        match   = re.search(r'\{.*\}', clean, re.DOTALL)
        if not match:
            log("hold_overnight check: no JSON returned — defaulting to close all.", "warning")
            return {}
        flags = json.loads(match.group())
        log("HOLD OVERNIGHT FLAGS:")
        for ticker, flag in flags.items():
            log(f"  {ticker}: {'HOLD ✓' if flag else 'close'}")
        return flags
    except Exception as e:
        log(f"hold_overnight check error: {e}", "error")
        return {}


async def get_grok_strategy(
    ctx: AppContext,
    watchlist: list,
    conversation: list,
    cfg: dict,
    investable: float = 9000.0,
    held_positions: dict = None,
) -> tuple[dict | None, str]:
    """
    Two-pass Grok strategy:
      Pass A — live X + web search → raw sentiment per ticker + SL news in one call
      Pass B — pure reasoning → conviction scores + USD allocations

    held_positions: optional dict of held position info for combined SL scan.
    Returns (strategy_dict | None, raw_news_str).
    strategy_dict has {ticker: pct_of_budget} plus "_reasoning" key.
    raw_news is passed back so get_sl_adjustments() can reuse it without a second Grok call.
    """

    log(f"[{datetime.datetime.now():%H:%M:%S}] Scanning news for {', '.join(watchlist)}...")

    # Optional user override — written by /say Telegram command or manually.
    # The bot picks this up each cycle so you can inject instructions without restarting.
    user_instructions = ""
    instructions_path = os.path.join(BOT_DIR, "bot_instructions.txt")
    if os.path.exists(instructions_path):
        with open(instructions_path) as f:
            user_instructions = f.read().strip()

    # Pull settings from config.json so they can be tuned without code changes
    gcfg         = cfg["grok"]
    risk_cfg     = cfg["risk"]
    caps         = cfg["allocation_caps"]
    min_score    = gcfg["min_conviction_score"]   # minimum score to place a buy (default 7)
    lookback_hrs = gcfg["news_lookback_hours"]     # how far back to look for news (default 1h)
    max_pos      = int(risk_cfg["max_single_position_pct"] * 100)  # max % of budget per stock
    cash_buf     = int(risk_cfg["cash_buffer_pct"] * 100)          # % to keep as cash reserve

    # ──────────────────────────────────────────────────────────────────────────
    # PASS A — live search
    # Grok searches X (cashtags) and whitelisted financial news sites.
    # Output: one line per ticker with direction, confidence, and top signal.
    #
    # TO TUNE: edit config.json → grok.focus_topics / grok.ignore_topics
    # TO ADD NEWS SOURCES: add domains to the web_search allowed_domains list below
    # ──────────────────────────────────────────────────────────────────────────
    search_targets = "\n".join(
        f"  {t}: X cashtag {TICKERS_CONTEXT[t][0]}, keywords: {TICKERS_CONTEXT[t][1]}"
        for t in watchlist if t in TICKERS_CONTEXT
    )

    # If we hold positions, include them in the same scan to save a Grok call.
    # The single response covers both buy signals and SL-relevant bearish news.
    held_section = ""
    if held_positions:
        held_lines = "\n".join(
            f"  {t}: entry=${info['entry']:.2f} | now=${info['current_price']:.2f} | SL=${info['sl']:.2f}"
            for t, info in held_positions.items() if t in TICKERS_CONTEXT
        )
        held_section = f"""
Also flag any STRONG bearish catalysts for these HELD positions (analyst downgrade with PT cut, earnings warning, SEC filing, breaking bad news):
{held_lines}
Add a line for each held ticker:
HELD_TICKER | BEARISH_SIGNAL/NONE | signal (1 sentence)"""

    search_prompt = f"""Scan X and web RIGHT NOW for trading signals. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}

First, report QQQ (Nasdaq 100 ETF) today's % change so far: QQQ | +X.XX% or -X.XX%

{search_targets}

For each ticker report ONE line:
TICKER | BULLISH/BEARISH/NEUTRAL/NODATA | HIGH/MEDIUM/LOW | top signal (1 sentence) | hard news if any

Priority signals: options flow, short squeeze, insider buy, earnings/guidance, analyst upgrade, product launch.
Look back {lookback_hrs}h max, prefer last 30min. Stale posts with no corroboration = NODATA.{held_section}"""

    try:
        search_response = ctx.client.responses.create(
            model=gcfg["model"],
            input=[{"role": "user", "content": sanitize(search_prompt)}],
            tools=[
                {"type": "x_search"},
                {
                    # TO ADD MORE SOURCES: append domains to this list
                    "type": "web_search",
                    "allowed_domains": ["reuters.com", "bloomberg.com", "cnbc.com", "sec.gov", "stocktwits.com"]
                },
            ],
        )
        raw_news = search_response.output_text
        log("SENTIMENT SCAN:")
        for line in raw_news.strip().splitlines():
            log(f"  {line}")

    except Exception as e:
        log(f"Grok search error: {e}", "error")
        return None, ""

    # ──────────────────────────────────────────────────────────────────────────
    # PASS B — pure reasoning, no search tools
    # Grok scores each ticker 1-10 and decides how many USD to allocate.
    # No live search here — it reasons only from Pass A output.
    #
    # TO TUNE SCORING: edit the score rubric in analysis_prompt below
    # TO CHANGE BEHAVIOUR: edit the mode_instruction strings
    # ──────────────────────────────────────────────────────────────────────────

    # Different instructions depending on whether we have cash to deploy or not.
    # When investable=0, bot runs in analysis-only mode (manages existing positions only).
    mode_instruction = (
        f"Available budget: ${investable:.0f} USD. Keep at least {cash_buf}% as cash — do NOT deploy it all unless signals are very strong."
        if investable > 0 else
        "NO cash available to buy. Do NOT allocate any USD to new positions. Focus only on whether existing held positions should be EXITED early based on bearish signals. Set usd_allocation to 0 for all tickers."
    )

    analysis_prompt = f"""Shariah-compliant tech trading bot. {datetime.datetime.now():%b %d %Y %H:%M ET}
Rules: long-only cash account, no shorting, no banks/weapons/gambling/alcohol.
{mode_instruction}
{f"User overrides: {user_instructions}" if user_instructions else ""}
IMPORTANT: If QQQ is down more than 1.5% today, set usd_allocation to 0 for ALL tickers — no new buys in a broad market selloff. Still score and reason normally, just allocate nothing.

NEWS:
{raw_news}

Score each stock 1-10:
9-10: hard news (CNBC/Reuters/SEC) + BULLISH + HIGH volume
7-8: hard news alone OR BULLISH HIGH + verified accounts
5-6: BULLISH MEDIUM, no hard news
≤4: weak/stale → skip
0: BEARISH or NODATA → skip

Only allocate to score ≥{min_score}. Decide how many USD to put into each qualifying stock — use your judgment based on signal strength. You don't have to use the full budget. Spread across multiple stocks if warranted.
IMPORTANT: Each allocation must be enough to buy at least 1 share. If the budget is small, concentrate on fewer stocks rather than splitting so thinly that no shares can be purchased. For example if budget is $1000 and GOOGL is $400, allocate at least $400 to GOOGL or skip it entirely.

Return ONLY valid JSON, no markdown, no extra keys, no trailing text. Use ONLY watchlist tickers as keys:
{{"conviction":{{"NVDA":0,"GOOGL":0}},"reasoning":["NVDA (0/10): reason"],"usd_allocation":{{"NVDA":0,"GOOGL":0}}}}
"""

    try:
        claude_cfg      = cfg.get("claude", {})
        reasoning_model = claude_cfg.get("model", "claude-sonnet-4-6")
        analysis_response = ctx.claude.messages.create(
            model=reasoning_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": analysis_prompt}],
        )
        content = analysis_response.content[0].text
        log(f"  [Pass B model: {reasoning_model}] raw output length: {len(content)} chars")

        # Strip markdown code fences if model wraps the JSON in ```json ... ```
        clean = re.sub(r'```(?:json)?|```', '', content).strip()
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if not match:
            log(f"Pass B ({reasoning_model}) returned no JSON — full output:\n{content}", "error")
            log("  Falling back to hold-only mode (no new buys this cycle).", "warning")
            return None, raw_news

        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as je:
            log(f"Pass B ({reasoning_model}) returned invalid JSON: {je}\n{match.group()}", "error")
            log("  Falling back to hold-only mode (no new buys this cycle).", "warning")
            return None, raw_news

        conviction = data.get("conviction", {})
        reasoning  = data.get("reasoning", [])
        usd_alloc  = data.get("usd_allocation", {})

        if not conviction and not usd_alloc:
            log(f"Pass B ({reasoning_model}) returned empty conviction/allocation — possible model degradation.", "warning")

        log("CLAUDE ANALYSIS:")
        for r in (reasoning if isinstance(reasoning, list) else [reasoning]):
            log(f"  {r}")

        # Convert USD allocations → % of investable budget, apply caps
        result = {}
        for ticker, usd in usd_alloc.items():
            if ticker not in watchlist:
                continue
            score = conviction.get(ticker, 0)
            if score < min_score:
                log(f"  {ticker}: conviction {score} < {min_score} threshold — skip.")
                continue
            if usd <= 0 or investable <= 0:
                continue
            # Cap per-stock allocation at: max_single_position_pct OR allocation_caps[ticker]
            pct = min((usd / investable) * 100, max_pos, caps.get(ticker, max_pos))
            result[ticker] = pct
            log(f"  {ticker}: conviction {score} → ${usd:.0f} ({pct:.1f}% of budget)")

        if not result:
            log("  No qualifying signals this cycle.")
            return None, raw_news

        log(f"  Qualifying signals: {list(result.keys())}")
        result["_reasoning"] = reasoning
        result["_conviction"] = {t: conviction.get(t, 0) for t in result if not t.startswith("_")}
        return result, raw_news

    except Exception as e:
        log(f"Grok analysis error: {e}", "error")
        return None, raw_news


async def get_sl_adjustments(
    ctx: AppContext,
    held_positions: dict,
    cfg: dict,
    raw_news: str = "",
) -> dict:
    """
    Ask Claude whether to tighten SL or exit immediately for held positions.

    held_positions: {ticker: {"qty": int, "entry": float, "current_price": float, "sl": float, "tp": float}}

    Returns {ticker: {"sl": float | None, "exit": bool}}
      - sl: new SL price, or None = no change
      - exit: True = market-sell immediately (strong bearish catalyst)
    Tickers not in result = no action.
    """
    if not held_positions or not raw_news:
        return {}

    positions_lines = "\n".join(
        f"  {t}: {info['qty']}sh | entry=${info['entry']:.2f} | now=${info['current_price']:.2f} | "
        f"SL=${info['sl']:.2f} | TP=${info['tp']:.2f} | P&L=${info['current_price']*info['qty'] - info['entry']*info['qty']:+.2f}"
        for t, info in held_positions.items()
    )

    prompt = f"""You are managing stop-losses for active stock positions. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}

HELD POSITIONS:
{positions_lines}

LIVE NEWS:
{raw_news}

Decide for each position: tighten SL or do nothing. Never exit mid-session — let the SL handle it.

IMPORTANT: Only act on news from TODAY ({datetime.datetime.now():%Y-%m-%d}). Ignore any article or post dated before today — treat it as null regardless of content.

TIGHTEN SL AGGRESSIVELY (tight: true) — strong bearish catalyst:
- Earnings warning / guidance cut confirmed by major outlet
- Double downgrade with large PT cut (>20%)
- SEC investigation, CEO resignation, breaking fundamental bad news
- Set new SL 1% below current price — very tight, price will hit it fast if news is real

TIGHTEN SL MODERATELY (tight: false) — moderate bearish news:
- Single analyst downgrade with PT cut
- Sector weakness confirmed by multiple sources
- Set new SL 2% below current price

DO NOTHING (null) — anything else:
- Vague sentiment, technical opinions, minor dips, speculative commentary
- BULLISH or NEUTRAL news
- When in doubt → null

Return ONLY valid JSON. Use null for no action:
{{"AMD": {{"sl": 338.50, "tight": true}}, "GOOGL": {{"sl": 395.00, "tight": false}}, "NVDA": null}}
"""

    try:
        model    = cfg.get("claude", {}).get("model", "claude-sonnet-4-6")
        response = ctx.claude.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text
        clean   = re.sub(r'```(?:json)?|```', '', content).strip()
        match   = re.search(r'\{.*\}', clean, re.DOTALL)
        if not match:
            log("SL adjustment: no JSON returned — keeping existing stops.", "warning")
            return {}
        raw = json.loads(match.group())

        result = {}
        for t, v in raw.items():
            if v is None:
                continue
            if isinstance(v, dict):
                sl    = v.get("sl")
                tight = bool(v.get("tight", False))
                if sl is not None:
                    result[t] = {"sl": sl, "tight": tight}
            elif isinstance(v, (int, float)):
                result[t] = {"sl": float(v), "tight": False}

        if result:
            log("SL ADJUSTMENTS:")
            for ticker, action in result.items():
                old_sl = held_positions.get(ticker, {}).get("sl", 0)
                label  = "AGGRESSIVE" if action["tight"] else "moderate"
                log(f"  {ticker}: {label} tighten SL ${old_sl:.2f} → ${action['sl']:.2f}")
        else:
            log("  SL scan: no adjustments needed this cycle.")
        return result
    except Exception as e:
        log(f"SL adjustment error: {e}", "error")
        return {}


async def get_premarket_scan(
    ctx: AppContext,
    held_positions: dict,
    cfg: dict,
) -> dict:
    """
    Pre-market scan — runs ~30 min before open (9:00 AM ET).
    One Grok news call on held positions, Claude decides per ticker:
      "hold" | "sell_at_open" | "tighten_sl"

    held_positions: {ticker: {"qty": int, "entry": float, "sl": float, "tp": float}}
    Returns {ticker: {"action": str, "new_sl": float | None}}
    """
    if not held_positions:
        return {}

    gcfg = cfg.get("grok", {})
    tickers = list(held_positions.keys())

    search_targets = "\n".join(
        f"  {t}: X cashtag {TICKERS_CONTEXT[t][0]}, keywords: {TICKERS_CONTEXT[t][1]}"
        for t in tickers if t in TICKERS_CONTEXT
    )

    search_prompt = f"""Pre-market research for held stock positions. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}
Market opens in ~30 minutes. Research overnight and pre-market news for these positions:

{search_targets}

For each ticker report:
TICKER | BULLISH/BEARISH/NEUTRAL | any major overnight event (earnings, guidance, downgrade, SEC, CEO news, macro) | pre-market price move if available

Focus on: overnight earnings releases, analyst actions, SEC filings, macro events (Fed, CPI), pre-market price action.
Look back 12 hours."""

    raw_news = ""
    try:
        search_response = ctx.client.responses.create(
            model=gcfg.get("model", "grok-4"),
            input=[{"role": "user", "content": sanitize(search_prompt)}],
            tools=[
                {"type": "x_search"},
                {"type": "web_search", "allowed_domains": ["reuters.com", "bloomberg.com", "cnbc.com", "sec.gov", "stocktwits.com"]},
            ],
        )
        raw_news = search_response.output_text
        log("PRE-MARKET SCAN:")
        for line in raw_news.strip().splitlines():
            log(f"  {line}")
    except Exception as e:
        log(f"Pre-market Grok scan error: {e}", "warning")
        return {}

    positions_lines = "\n".join(
        f"  {t}: {info['qty']}sh @ ${info['entry']:.2f} | SL=${info['sl']:.2f} | TP={info.get('tp', 0):.2f}"
        for t, info in held_positions.items()
    )

    prompt = f"""You are deciding what to do with held stock positions at market open. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}

HELD POSITIONS:
{positions_lines}

OVERNIGHT / PRE-MARKET NEWS:
{raw_news}

For each position choose ONE action:

"sell_at_open" — sell immediately when market opens. Use for:
  - Earnings miss / guidance cut confirmed
  - SEC investigation, fraud, CEO resignation
  - Double downgrade with large PT cut (>20%)
  - Strong gap-down pre-market (>3%) with confirmed bad news
  - Any event that fundamentally changes the investment thesis negatively

"tighten_sl" — keep position but move SL closer. Use for:
  - Single downgrade, mild negative news, sector weakness
  - Small pre-market gap-down (<3%) with uncertain cause
  - Provide new_sl at least 2% below current/pre-market price

"hold" — no change. Use for:
  - Bullish or neutral news
  - No significant overnight events
  - When in doubt → hold

Return ONLY valid JSON:
{{"AMD": {{"action": "hold", "new_sl": null}}, "NVDA": {{"action": "sell_at_open", "new_sl": null}}, "GOOGL": {{"action": "tighten_sl", "new_sl": 385.00}}}}
"""

    try:
        model    = cfg.get("claude", {}).get("model", "claude-sonnet-4-6")
        response = ctx.claude.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text
        clean   = re.sub(r'```(?:json)?|```', '', content).strip()
        match   = re.search(r'\{.*\}', clean, re.DOTALL)
        if not match:
            log("Pre-market scan: no JSON returned — defaulting to hold all.", "warning")
            return {}
        data = json.loads(match.group())

        result = {}
        for ticker, v in data.items():
            if not isinstance(v, dict):
                continue
            action  = v.get("action", "hold")
            new_sl  = v.get("new_sl")
            result[ticker] = {"action": action, "new_sl": new_sl}
            if action == "sell_at_open":
                log(f"  {ticker}: SELL AT OPEN — flagged by pre-market scan")
            elif action == "tighten_sl" and new_sl:
                log(f"  {ticker}: tighten SL → ${new_sl:.2f} at open")
            else:
                log(f"  {ticker}: hold — no action needed")
        return result

    except Exception as e:
        log(f"Pre-market scan Claude error: {e}", "error")
        return {}
