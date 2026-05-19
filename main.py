import asyncio
import datetime
import os
import socket
import subprocess
import time
from config import IB_HOST, IB_PORT, load_config, create_context, TRADING_MODE
from logger import log
from helpers import (
    is_market_open, shutdown_requested, start_keyboard_listener,
    next_market_open_secs, kuwait_time_str, is_eod_close_window, is_friday_eod,
)
from account import get_total_capital
from risk import RiskGuard
from strategy import get_grok_strategy, get_hold_overnight_flags, get_premarket_scan
from trading import trade_rebalance, close_all_positions, check_for_stopouts


TWS_APP = "/Users/mac/Applications/Trader Workstation/Trader Workstation.app"
_TWS_LAUNCH_TIMEOUT = 300  # seconds to wait for TWS login + phone approval


def _tws_port_open() -> bool:
    try:
        s = socket.create_connection((IB_HOST, IB_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _tws_process_running() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", "Trader Workstation"], text=True)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


_VENV_PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python3")


def _ensure_tws_running():
    if _tws_port_open():
        log("TWS API port is open — already running.")
        return

    log("TWS not ready — running auto-login...")
    login_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tws_login.py")
    result = subprocess.run([_VENV_PYTHON, login_script])
    if result.returncode != 0 or not _tws_port_open():
        raise RuntimeError(
            f"TWS did not open API port {IB_PORT} within timeout. "
            "Check TWS is running and API is enabled, or log in manually first via the dashboard."
        )


def _print_banner():
    print()
    print("  ╔" + "═" * 54 + "╗")
    print("  ║" + " " * 54 + "║")
    print("  ║" + "     ██╗ ██████╗ ███████╗    ██████╗  ██████╗ ████████╗  ".center(54) + "║")
    print("  ║" + "     ██║██╔═══██╗██╔════╝    ██╔══██╗██╔═══██╗╚══██╔══╝  ".center(54) + "║")
    print("  ║" + "     ██║██║   ██║█████╗      ██████╔╝██║   ██║   ██║     ".center(54) + "║")
    print("  ║" + "██   ██║██║   ██║██╔══╝      ██╔══██╗██║   ██║   ██║     ".center(54) + "║")
    print("  ║" + "╚█████╔╝╚██████╔╝███████╗    ██████╔╝╚██████╔╝   ██║     ".center(54) + "║")
    print("  ║" + " ╚════╝  ╚═════╝ ╚══════╝    ╚═════╝  ╚═════╝    ╚═╝     ".center(54) + "║")
    print("  ║" + " " * 54 + "║")
    print("  ║" + "     Shariah-Compliant AI Trading Bot".center(54) + "║")
    print("  ║" + "           Powered by Grok + IBKR".center(54) + "║")
    print("  ║" + " " * 54 + "║")
    print("  ╚" + "═" * 54 + "╝")
    print()


async def _write_daily_summary(ctx, risk, cfg, held_overnight: dict):
    """
    Asks Claude to write a daily summary to progress/YYYY-MM-DD.md and update trades.csv.
    Called once at EOD after positions are closed.
    """
    import os
    from config import BOT_DIR

    today     = datetime.date.today().isoformat()
    prog_path = os.path.join(BOT_DIR, "progress", f"{today}.md")
    csv_path  = os.path.join(BOT_DIR, "progress", "trades.csv")

    # Build session summary for Claude
    trades_lines = []
    for ticker, s in risk.session_trades.items():
        trades_lines.append(
            f"  {ticker}: {s['qty']}sh @ ${s['fill_price']:.2f} | SL=${s['sl']:.2f} TP={s.get('tp', 0):.2f} | "
            f"hold_overnight={s.get('hold_overnight', False)}"
        )

    # Read last 60 lines of log for context
    log_path = os.path.join(BOT_DIR, "bot_activity.log")
    try:
        with open(log_path) as f:
            log_tail = "".join(f.readlines()[-60:])
    except Exception:
        log_tail = "(log unavailable)"

    # Read existing trades.csv
    try:
        with open(csv_path) as f:
            existing_csv = f.read()
    except Exception:
        existing_csv = "date,ticker,entry_price,exit_price,qty,pnl,exit_reason,hold_minutes\n"

    held_str = ", ".join(f"{t} ({'HOLD' if v else 'closed'})" for t, v in held_overnight.items()) or "none"

    prompt = f"""You are writing the end-of-day summary for a trading bot. Date: {today}

SESSION TRADES (open positions):
{chr(10).join(trades_lines) if trades_lines else "  none"}

HELD OVERNIGHT: {held_str}

RECENT LOG (last 60 lines):
{log_tail}

EXISTING trades.csv:
{existing_csv}

Tasks:
1. Write a SHORT daily summary in this exact format (no extra text):
---SUMMARY---
# {today}
Net P&L: <realized> realized | <unrealized> unrealized
Closed: <ticker> <reason> <pnl>, ...  (or "none")
Held overnight: <tickers> (or "none")
Bugs: <any bugs seen in logs, or "none">
Notes: <1 line of anything notable>
API cost: <estimate from log call count>
---END---

2. List any NEW closed trades to append to trades.csv (trades closed TODAY not already in the csv) in this format:
---CSV---
date,ticker,entry_price,exit_price,qty,pnl,exit_reason,hold_minutes
(rows here, or NONE if nothing to add)
---END---

Be precise with numbers. If you can't determine a value write "?".
"""

    try:
        model    = cfg.get("claude", {}).get("model", "claude-sonnet-4-6")
        response = ctx.claude.messages.create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text

        # Extract and write summary
        import re
        summary_match = re.search(r'---SUMMARY---(.*?)---END---', content, re.DOTALL)
        if summary_match:
            summary = summary_match.group(1).strip()
            with open(prog_path, "w") as f:
                f.write(summary + "\n")
            log(f"  Daily summary written to progress/{today}.md")

        # Extract and append CSV rows
        csv_match = re.search(r'---CSV---(.*?)---END---', content, re.DOTALL)
        if csv_match:
            csv_block = csv_match.group(1).strip()
            lines = [l for l in csv_block.splitlines() if l.strip() and l.strip() != "NONE" and not l.startswith("date,")]
            if lines:
                with open(csv_path, "a") as f:
                    for line in lines:
                        f.write(line + "\n")
                log(f"  {len(lines)} trade(s) appended to trades.csv")

    except Exception as e:
        log(f"  Daily summary error: {e}", "error")


async def _refresh_overnight_sl_tp(ctx, risk, cfg, flags: dict):
    """
    For each position flagged hold_overnight=True, cancel existing SL/TP and
    place fresh ones based on current ATR. Called at EOD before close_all_positions.
    """
    from ib_async import Stock, StopOrder, LimitOrder
    from atr import get_atr
    import asyncio as _asyncio

    held = [t for t, flag in flags.items() if flag and t in risk.session_trades]
    if not held:
        return

    open_orders = await ctx.ib.reqAllOpenOrdersAsync()
    protection  = {}
    for t in open_orders:
        sym = t.contract.symbol
        if sym in held and t.orderStatus.status not in ('Cancelled', 'Filled', 'Inactive'):
            protection.setdefault(sym, []).append(t)

    positions = {p.contract.symbol: p.position for p in await ctx.ib.reqPositionsAsync()}

    for ticker in held:
        qty = int(positions.get(ticker, 0))
        if qty <= 0:
            continue

        contract = Stock(ticker, 'SMART', 'USD')
        await ctx.ib.qualifyContractsAsync(contract)
        ctx.ib.reqMarketDataType(1)
        [td] = await ctx.ib.reqTickersAsync(contract)
        price = td.marketPrice() or td.last or td.close
        if not price or price <= 0:
            log(f"  {ticker}: bad price at EOD — keeping existing SL/TP.", "warning")
            continue

        atr = await get_atr(ctx, contract, cfg)
        if atr is None:
            continue

        sl_price  = round(price - atr * cfg["atr"]["sl_multiplier"], 2)
        tp_price  = round(price + atr * cfg["atr"]["tp_multiplier"], 2)
        oca_group = f"OCA_{ticker}_overnight_{int(_asyncio.get_event_loop().time())}"

        for o in protection.get(ticker, []):
            ctx.ib.cancelOrder(o.order)
        await _asyncio.sleep(0.5)

        sl = StopOrder('SELL', qty, sl_price, account=ctx.ib_acc, tif='GTC')
        tp = LimitOrder('SELL', qty, tp_price, account=ctx.ib_acc, tif='GTC')
        sl.ocaGroup = oca_group; sl.ocaType = 1; sl.transmit = True
        tp.ocaGroup = oca_group; tp.ocaType = 1; tp.transmit = True
        ctx.ib.placeOrder(contract, sl)
        ctx.ib.placeOrder(contract, tp)

        risk.session_trades[ticker]['sl'] = sl_price
        risk.session_trades[ticker]['tp'] = tp_price
        log(f"  {ticker}: overnight SL=${sl_price} TP=${tp_price} refreshed ({qty}sh @ ${price:.2f})")


async def main():
    import helpers  # for mutating shutdown_requested

    _print_banner()
    ctx = create_context()
    start_keyboard_listener()

    log(f"Mode: {'📄 PAPER TRADING' if TRADING_MODE == 'paper' else '💰 LIVE TRADING'}")
    _ensure_tws_running()
    log(f"Connecting to IBKR at {IB_HOST}:{IB_PORT}...")
    await ctx.ib.connectAsync(IB_HOST, IB_PORT, clientId=15, timeout=60)
    log("Connected.")
    log(f"Press  x + Enter  to safely shut down.\n")

    cfg = load_config()
    starting_equity = await get_total_capital(ctx)
    risk = RiskGuard(
        starting_equity,
        cfg["risk"]["daily_loss_limit_pct"],
        cfg["risk"]["cooldown_cycles_after_stopout"],
    )
    current_day = datetime.date.today()
    log(f"Risk guard | equity: ${starting_equity:,.2f} | "
        f"loss limit: ${risk.loss_limit:,.2f} ({cfg['risk']['daily_loss_limit_pct']*100:.0f}%)")

    # Hook into IBKR execution events to capture real fill prices for SL/TP hits.
    # This fires immediately when any order fills — including automatic SL/TP orders.
    # We store {ticker: (fill_price, commission)} so check_for_stopouts can log accurately.
    from logger import log_trade_csv
    def _on_exec_details(trade, fill):
        sym    = fill.contract.symbol
        action = fill.execution.side  # 'BOT' or 'SLD'
        price  = fill.execution.avgPrice
        if action == 'SLD' and sym in risk.session_trades:
            risk.session_trades[sym]['last_exit_price'] = price
            log(f"  {sym}: SELL fill captured @ ${price:.2f}")

    ctx.ib.execDetailsEvent += _on_exec_details

    prev_positions = {p.contract.symbol: p.position
                      for p in await ctx.ib.reqPositionsAsync()}

    # Clean stale session_trades — remove any ticker no longer held in IBKR
    # Prevents bot from thinking it holds positions that were already closed/trimmed
    for ticker in list(risk.session_trades.keys()):
        if prev_positions.get(ticker, 0) <= 0:
            log(f"  Startup: {ticker} in session_trades but not held in IBKR — removing.")
            risk.close_session_trade(ticker)

    # Start Telegram bot if configured
    tg = None
    try:
        from telegram_bot import TelegramBot
        tg = TelegramBot(ctx, risk)
        await tg.start()
        log("Telegram bot started.")
    except Exception as e:
        log(f"Telegram bot not started: {e}")

    conversation: list = []

    try:
        while True:
            if helpers.shutdown_requested:
                await close_all_positions(ctx, cfg["watchlist"], "user pressed x", risk)
                break

            cfg        = load_config()
            watchlist  = cfg["watchlist"]
            cycle_secs = cfg["timing"]["cycle_seconds"]

            cycle_start = datetime.datetime.now()
            log(f"\n{'=' * 60}")
            log(f"CYCLE  {cycle_start:%Y-%m-%d %H:%M:%S}")
            log(f"{'=' * 60}")

            # Reconnect if IBKR connection dropped silently
            if not ctx.ib.isConnected():
                log("IBKR connection lost — reconnecting...", "warning")
                try:
                    await ctx.ib.connectAsync(IB_HOST, IB_PORT, clientId=15, timeout=60)
                    log("Reconnected to IBKR.")
                except Exception as e:
                    log(f"Reconnect failed: {e} — sleeping 60s.", "error")
                    await asyncio.sleep(60)
                    continue

            # New trading day reset
            if datetime.date.today() != current_day:
                current_day = datetime.date.today()
                new_equity  = await get_total_capital(ctx)
                risk.reset(new_equity)
                conversation = []

            risk.tick_cooldowns()
            prev_positions = await check_for_stopouts(ctx, risk, prev_positions, watchlist)

            if not is_market_open():
                secs_left = next_market_open_secs()

                # Pre-market scan — fires once when 25-35 min from open
                # Uses ALL held IBKR positions, not just session_trades
                if 25 * 60 <= secs_left <= 35 * 60:
                    already_scanned = getattr(risk, '_premarket_scanned_date', None)
                    if already_scanned != datetime.date.today():
                        _positions_snap = {p.contract.symbol: p.position
                                           for p in await ctx.ib.reqPositionsAsync()}
                        held_for_scan = {}
                        for sym, qty in _positions_snap.items():
                            if qty > 0 and sym in watchlist:
                                session = risk.session_trades.get(sym)
                                held_for_scan[sym] = {
                                    "qty":   int(qty),
                                    "entry": session["fill_price"] if session else 0,
                                    "sl":    session["sl"] if session else 0,
                                    "tp":    session.get("tp", 0) if session else 0,
                                }
                        if held_for_scan:
                            log(f"  PRE-MARKET SCAN — checking overnight news on {', '.join(held_for_scan)}...")
                            pm_flags = await get_premarket_scan(ctx, held_for_scan, cfg)
                            from risk import _save_session_trades as _save_st
                            for ticker, decision in pm_flags.items():
                                if ticker not in risk.session_trades:
                                    # create minimal entry so premarket flags can be stored
                                    risk.session_trades[ticker] = {
                                        "qty": held_for_scan[ticker]["qty"],
                                        "fill_price": held_for_scan[ticker]["entry"],
                                        "sl": held_for_scan[ticker]["sl"],
                                        "tp": held_for_scan[ticker]["tp"],
                                        "entry_dt": datetime.datetime.now().isoformat(),
                                    }
                                risk.session_trades[ticker]['premarket_action'] = decision['action']
                                if decision.get('new_sl'):
                                    risk.session_trades[ticker]['premarket_new_sl'] = decision['new_sl']
                            _save_st(risk.session_trades)
                        risk._premarket_scanned_date = datetime.date.today()

                while secs_left > 0:
                    if helpers.shutdown_requested:
                        break
                    h, rem = divmod(secs_left, 3600)
                    m, s   = divmod(rem, 60)
                    print(f"\r  Market closed | Kuwait: {kuwait_time_str()}  |  "
                          f"Opens in {h:02d}:{m:02d}:{s:02d}  ", end="", flush=True)
                    await asyncio.sleep(1)
                    secs_left -= 1
                print()
                continue

            else:
                from account import get_available_cash
                from ib_async import Stock, MarketOrder as _MarketOrder, StopOrder as _StopOrder

                # Execute pre-market sell-at-open flags on first cycle after open
                for ticker, session in list(risk.session_trades.items()):
                    action = session.pop('premarket_action', None)
                    new_sl = session.pop('premarket_new_sl', None)
                    if action == 'sell_at_open':
                        _positions_snap = {p.contract.symbol: p.position
                                           for p in await ctx.ib.reqPositionsAsync()}
                        qty = int(_positions_snap.get(ticker, 0))
                        if qty <= 0:
                            log(f"  {ticker}: sell_at_open flagged but no shares held — skipping.")
                            risk.close_session_trade(ticker)
                            continue
                        log(f"  {ticker}: SELL AT OPEN — executing {qty}sh (pre-market flag)")
                        contract = Stock(ticker, 'SMART', 'USD')
                        await ctx.ib.qualifyContractsAsync(contract)
                        # Cancel existing SL/TP first
                        for t in await ctx.ib.reqAllOpenOrdersAsync():
                            if t.contract.symbol == ticker and t.order.action == 'SELL':
                                ctx.ib.cancelOrder(t.order)
                        await asyncio.sleep(0.5)
                        sell_order          = _MarketOrder('SELL', qty, account=ctx.ib_acc)
                        sell_order.tif      = 'DAY'
                        sell_order.transmit = True
                        ctx.ib.placeOrder(contract, sell_order)
                        log(f"  {ticker}: sell-at-open order placed.")
                        risk.close_session_trade(ticker)
                    elif action == 'tighten_sl' and new_sl:
                        log(f"  {ticker}: applying pre-market SL tighten → ${new_sl:.2f}")
                        risk.session_trades[ticker]['sl'] = new_sl
                        from risk import _save_session_trades as _save_st
                        _save_st(risk.session_trades)

                import zoneinfo as _zi

                eod_cfg    = cfg.get("eod", {})
                close_mins = eod_cfg.get("close_minutes_before", 15)

                # If EOD window is <2 min away but not yet started, wait for it so the
                # Grok scan can't eat into it.  Check this BEFORE cash calculations.
                _et_now    = datetime.datetime.now(datetime.timezone.utc).astimezone(_zi.ZoneInfo("America/New_York"))
                _et_close  = _et_now.replace(hour=16, minute=0, second=0, microsecond=0)
                _eod_start = _et_close - datetime.timedelta(minutes=close_mins)
                _secs_to_eod = (_eod_start - _et_now).total_seconds()
                if 0 < _secs_to_eod < 120:
                    log(f"  EOD window in {int(_secs_to_eod)}s — waiting before scan...")
                    await asyncio.sleep(_secs_to_eod + 5)
                    # Recompute after wait
                    _et_now = datetime.datetime.now(datetime.timezone.utc).astimezone(_zi.ZoneInfo("America/New_York"))

                # EOD close — before market close on any day (checked BEFORE Grok scan)
                if eod_cfg.get("close_all_eod") and is_eod_close_window(close_mins):
                    log("  EOD window — asking Claude which positions to hold overnight...")
                    portfolio_items = {p.contract.symbol: p for p in ctx.ib.portfolio()}
                    # Include ALL held positions, not just session_trades
                    current_positions = {}
                    for sym, item in portfolio_items.items():
                        if item.position <= 0 or sym not in watchlist:
                            continue
                        session = risk.session_trades.get(sym)
                        current_positions[sym] = {
                            "qty":   int(item.position),
                            "entry": session["fill_price"] if session else item.averageCost,
                            "price": item.marketPrice,
                            "pnl":   item.unrealizedPNL,
                        }
                    flags = await get_hold_overnight_flags(ctx, current_positions, cfg)
                    for t, flag in flags.items():
                        if t in risk.session_trades:
                            risk.session_trades[t]['hold_overnight'] = flag
                        else:
                            # pre-existing position — create a minimal session entry so
                            # close_all_positions can respect the hold_overnight flag
                            risk.session_trades[t] = {
                                "qty": current_positions[t]["qty"],
                                "fill_price": current_positions[t]["entry"],
                                "sl": 0, "tp": 0,
                                "entry_dt": datetime.datetime.now().isoformat(),
                                "hold_overnight": flag,
                            }
                    await _refresh_overnight_sl_tp(ctx, risk, cfg, flags)
                    log("  Closing non-held positions before market close.")
                    await close_all_positions(ctx, watchlist, "end of day", risk)
                    await _write_daily_summary(ctx, risk, cfg, flags)
                    risk.reset(await get_total_capital(ctx))
                    log(f"Cycle done. Sleeping {cfg['timing']['cycle_seconds']}s...")
                    await asyncio.sleep(cfg["timing"]["cycle_seconds"])
                    continue

                # Friday hard close — no weekend risk (checked BEFORE Grok scan)
                if eod_cfg.get("close_all_friday") and is_friday_eod(close_mins):
                    log("  Friday EOD — asking Claude which positions to hold over weekend...")
                    portfolio_items = {p.contract.symbol: p for p in ctx.ib.portfolio()}
                    # Include ALL held positions, not just session_trades
                    current_positions = {}
                    for sym, item in portfolio_items.items():
                        if item.position <= 0 or sym not in watchlist:
                            continue
                        session = risk.session_trades.get(sym)
                        current_positions[sym] = {
                            "qty":   int(item.position),
                            "entry": session["fill_price"] if session else item.averageCost,
                            "price": item.marketPrice,
                            "pnl":   item.unrealizedPNL,
                        }
                    flags = await get_hold_overnight_flags(ctx, current_positions, cfg, is_friday=True)
                    for t, flag in flags.items():
                        if t in risk.session_trades:
                            risk.session_trades[t]['hold_overnight'] = flag
                        else:
                            risk.session_trades[t] = {
                                "qty": current_positions[t]["qty"],
                                "fill_price": current_positions[t]["entry"],
                                "sl": 0, "tp": 0,
                                "entry_dt": datetime.datetime.now().isoformat(),
                                "hold_overnight": flag,
                            }
                    await _refresh_overnight_sl_tp(ctx, risk, cfg, flags)
                    log("  Closing non-held positions for weekend.")
                    await close_all_positions(ctx, watchlist, "Friday close", risk)
                    await _write_daily_summary(ctx, risk, cfg, flags)
                    risk.reset(await get_total_capital(ctx))
                    log(f"Cycle done. Sleeping {cfg['timing']['cycle_seconds']}s...")
                    await asyncio.sleep(cfg["timing"]["cycle_seconds"])
                    continue

                available_cash = await get_available_cash(ctx)
                cap_usd        = cfg["risk"].get("portfolio_cap_usd")
                total_capital  = await get_total_capital(ctx)

                # Free budget = cash we actually have minus the buffer reserve.
                # portfolio_cap_usd caps total portfolio value (deployed + free),
                # so free = cap - already_deployed (but never more than available cash).
                cash_buffer = cfg["risk"]["cash_buffer_pct"]
                spendable   = available_cash * (1.0 - cash_buffer)
                if cap_usd:
                    already_in_market = total_capital - available_cash
                    room_under_cap    = max(0.0, cap_usd - already_in_market)
                    free_budget       = min(spendable, room_under_cap)
                else:
                    free_budget = spendable

                free_budget = max(0.0, free_budget)
                log(f"  Cash: ${available_cash:.0f} | buffer: {int(cash_buffer*100)}% | "
                    f"spendable: ${spendable:.0f} | free budget: ${free_budget:.0f}")

                MIN_BUY_BUDGET = 150.0
                scan_budget = free_budget if free_budget >= MIN_BUY_BUDGET else 0.0

                # Build held_info for combined scan — ALL held IBKR positions,
                # not just session_trades, so pre-existing positions get news evaluated too
                held_info_for_scan = {}
                _positions_snap = {p.contract.symbol: p.position for p in await ctx.ib.reqPositionsAsync()}
                for sym, qty in _positions_snap.items():
                    if qty > 0 and sym in watchlist:
                        session = risk.session_trades.get(sym)
                        held_info_for_scan[sym] = {
                            "qty":           int(qty),
                            "entry":         session["fill_price"] if session else 0,
                            "current_price": session["fill_price"] if session else 0,
                            "sl":            session["sl"] if session else 0,
                            "tp":            session.get("tp", 0) if session else 0,
                        }

                if scan_budget == 0.0 and not held_info_for_scan:
                    log("  No free cash and no held positions — skipping cycle.")
                    await trade_rebalance(ctx, {}, risk, cfg, [])
                    # Still use smart sleep so EOD window isn't missed
                    _et_now_s  = datetime.datetime.now(datetime.timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo("America/New_York"))
                    _et_cls_s  = _et_now_s.replace(hour=16, minute=0, second=0, microsecond=0)
                    _eod_s     = _et_cls_s - datetime.timedelta(minutes=cfg.get("eod", {}).get("close_minutes_before", 15))
                    _secs_eod  = (_eod_s - _et_now_s).total_seconds()
                    if 0 < _secs_eod < cycle_secs:
                        _sl = max(30, int(_secs_eod) - 10)
                        log(f"Cycle done. EOD in {int(_secs_eod)}s — sleeping {_sl}s to catch window.")
                        await asyncio.sleep(_sl)
                    else:
                        log(f"Cycle done. Sleeping {cycle_secs}s...")
                        await asyncio.sleep(cycle_secs)
                    continue

                raw, raw_news = await get_grok_strategy(
                    ctx, watchlist, conversation, cfg, scan_budget,
                    held_positions=held_info_for_scan or None,
                )

                if raw:
                    reasoning = raw.pop("_reasoning", [])
                    await trade_rebalance(ctx, raw, risk, cfg, reasoning, raw_news=raw_news)
                else:
                    log("  No actionable signal.")
                    await trade_rebalance(ctx, {}, risk, cfg, [], raw_news=raw_news)

            # Sleep until next cycle, but wake up early if EOD window is approaching
            import zoneinfo as _zi
            _et_now   = datetime.datetime.now(datetime.timezone.utc).astimezone(_zi.ZoneInfo("America/New_York"))
            _et_close = _et_now.replace(hour=16, minute=0, second=0, microsecond=0)
            _eod_start = _et_close - datetime.timedelta(minutes=cfg.get("eod", {}).get("close_minutes_before", 15))
            _secs_to_eod = (_eod_start - _et_now).total_seconds()
            if 0 < _secs_to_eod < cycle_secs:
                sleep_secs = max(30, int(_secs_to_eod) - 10)
                log(f"Cycle done. EOD in {int(_secs_to_eod)}s — sleeping {sleep_secs}s to catch window.")
            else:
                sleep_secs = cycle_secs
                log(f"Cycle done. Sleeping {sleep_secs}s...")
            await asyncio.sleep(sleep_secs)

    except KeyboardInterrupt:
        log("Ctrl-C received.")
        await close_all_positions(ctx, cfg["watchlist"], "Ctrl-C", risk)
    except Exception as e:
        log(f"Fatal error: {e}", "error")
        raise
    finally:
        if tg:
            await tg.stop()
        ctx.ib.disconnect()
        log("Disconnected from IBKR.")


if __name__ == "__main__":
    asyncio.run(main())
