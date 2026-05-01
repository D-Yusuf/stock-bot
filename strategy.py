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
        search_prompt = f"""Scan X and web RIGHT NOW for multi-day outlook on these stocks. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}

{search_targets}

For each ticker report ONE line:
TICKER | BULLISH/BEARISH/NEUTRAL | HIGH/MEDIUM/LOW | key catalyst for next 1-5 days (1 sentence) | any earnings, product launches, analyst events upcoming?

Focus on: upcoming earnings, analyst upgrades/targets, product launches, macro tailwinds, institutional activity.
Look back 4 hours max."""

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

    if is_friday:
        hold_criteria = """HOLD over weekend if: strong next-week catalyst confirmed (earnings beat, analyst upgrade, product launch Monday), position is profitable, momentum is intact, no major weekend risk events.
CLOSE before weekend if: no clear next-week catalyst, position in loss, bearish signals, geopolitical/regulatory risk, or earnings surprise risk that could gap down Monday.
Be MORE selective on Fridays — holding over a weekend means 3 days of gap risk. Only hold if the setup is genuinely strong."""
    else:
        hold_criteria = """HOLD if: upcoming earnings with strong outlook, major product launch, strong multi-day bullish catalyst, position is profitable with momentum.
CLOSE if: intraday-only signal, stale news, bearish catalyst, position in loss with no upcoming catalyst, high overnight risk.
Be willing to take calculated risks on strong multi-day setups — that's how trading makes money."""

    prompt = f"""You are reviewing {'end-of-week' if is_friday else 'end-of-day'} positions for a Shariah-compliant trading bot. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}
{'These positions would be held over the ENTIRE WEEKEND if flagged HOLD.' if is_friday else ''}

HELD POSITIONS:
{positions_lines}

{news_section}

{hold_criteria}

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
) -> dict | None:
    """
    Two-pass Grok strategy:
      Pass A — live X + web search → raw sentiment per ticker
      Pass B — pure reasoning → conviction scores + USD allocations

    Returns dict of {ticker: pct_of_budget} for qualifying tickers,
    plus "_reasoning" key with Grok's explanation list.
    Returns None if no qualifying signals.
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

    search_prompt = f"""Scan X and web RIGHT NOW for trading signals. Date: {datetime.datetime.now():%b %d %Y %H:%M ET}

First, report QQQ (Nasdaq 100 ETF) today's % change so far: QQQ | +X.XX% or -X.XX%

{search_targets}

For each ticker report ONE line:
TICKER | BULLISH/BEARISH/NEUTRAL/NODATA | HIGH/MEDIUM/LOW | top signal (1 sentence) | hard news if any

Priority signals: options flow, short squeeze, insider buy, earnings/guidance, analyst upgrade, product launch.
Look back {lookback_hrs}h max, prefer last 30min. Stale posts with no corroboration = NODATA."""

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
        return None

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
            return None

        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as je:
            log(f"Pass B ({reasoning_model}) returned invalid JSON: {je}\n{match.group()}", "error")
            log("  Falling back to hold-only mode (no new buys this cycle).", "warning")
            return None

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
            return None

        log(f"  Qualifying signals: {list(result.keys())}")
        result["_reasoning"] = reasoning
        return result

    except Exception as e:
        log(f"Grok analysis error: {e}", "error")
        return None
