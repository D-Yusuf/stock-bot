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
    "TSM":   ("$TSM",   "TSMC Taiwan Semiconductor"),
    "CRM":   ("$CRM",   "Salesforce Agentforce"),
    "ADBE":  ("$ADBE",  "Adobe Firefly"),
}


async def get_grok_strategy(
    ctx: AppContext,
    watchlist: list,
    conversation: list,
    cfg: dict,
    investable: float = 8000.0,
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

NEWS:
{raw_news}

Score each stock 1-10:
9-10: hard news (CNBC/Reuters/SEC) + BULLISH + HIGH volume
7-8: hard news alone OR BULLISH HIGH + verified accounts
5-6: BULLISH MEDIUM, no hard news
≤4: weak/stale → skip
0: BEARISH or NODATA → skip

Only allocate to score ≥{min_score}. Decide how many USD to put into each qualifying stock — use your judgment based on signal strength. You don't have to use the full budget. Spread across multiple stocks if warranted.

Return ONLY JSON, no markdown:
{{"conviction":{{"NVDA":0}},"reasoning":["NVDA (0/10): reason"],"usd_allocation":{{"NVDA":0}}}}
"""

    try:
        analysis_response = ctx.client.responses.create(
            model=gcfg["model"],
            input=[{"role": "user", "content": sanitize(analysis_prompt)}],
        )
        content = analysis_response.output_text

        # Strip markdown code fences if Grok wraps the JSON in ```json ... ```
        clean = re.sub(r'```(?:json)?|```', '', content).strip()
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if not match:
            log("Grok returned no JSON in analysis pass:\n" + content, "error")
            return None

        data       = json.loads(match.group())
        conviction = data.get("conviction", {})
        reasoning  = data.get("reasoning", [])
        usd_alloc  = data.get("usd_allocation", {})

        log("GROK ANALYSIS:")
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
