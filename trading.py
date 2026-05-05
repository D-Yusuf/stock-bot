import asyncio
import datetime
from ib_async import Stock, MarketOrder, StopOrder, LimitOrder
from config import AppContext
from logger import log, log_order, emit_event, log_trade_csv
from account import get_total_capital, get_available_cash
from atr import get_atr
from helpers import is_market_open, within_30min_of_open
from risk import RiskGuard
from strategy import get_sl_adjustments

# Minimum shares to bother buying. Prevents placing a 1-share order worth $5.
# Raise this if you want to avoid very small top-up buys.
MIN_DELTA = 1


async def close_all_positions(ctx: AppContext, watchlist: list, reason: str = "shutdown", risk: RiskGuard = None):
    log(f"CLOSING ALL POSITIONS — reason: {reason}")
    positions = {p.contract.symbol: p.position
                 for p in await ctx.ib.reqPositionsAsync()}

    for t in await ctx.ib.reqAllOpenOrdersAsync():
        sym = t.contract.symbol
        if sym not in watchlist:
            continue
        session = risk.session_trades.get(sym) if risk else None
        if session and session.get('hold_overnight') and reason in ("end of day", "Friday close"):
            continue
        ctx.ib.cancelOrder(t.order)
    await asyncio.sleep(1)

    for ticker, qty in positions.items():
        if ticker not in watchlist or qty <= 0:
            continue
        session = risk.session_trades.get(ticker) if risk else None
        if session and session.get('hold_overnight') and reason in ("end of day", "Friday close"):
            log(f"  {ticker}: hold_overnight=True — keeping position open.")
            continue
        contract = Stock(ticker, 'SMART', 'USD')
        await ctx.ib.qualifyContractsAsync(contract)
        log(f"  Closing {qty} x {ticker}")

        session  = risk.session_trades.get(ticker) if risk else None
        entry_dt = datetime.datetime.fromisoformat(
            session.get('entry_dt', datetime.datetime.now().isoformat())
        ) if session else datetime.datetime.now()

        sell_qty = int(qty)  # floor to whole shares — API rejects fractional market sells
        log_order("SELL (close)", ticker, sell_qty, 0.0, reason=[f"Position closed — {reason}"])
        emit_event("SELL", {"ticker": ticker, "qty": sell_qty, "reason": reason})
        order          = MarketOrder('SELL', sell_qty, account=ctx.ib_acc)
        order.tif      = 'DAY'
        order.transmit = True
        trade = ctx.ib.placeOrder(contract, order)

        # Wait for fill to get real exit price and commission
        deadline = asyncio.get_event_loop().time() + 30
        while not trade.isDone() and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)

        if trade.isDone() and trade.orderStatus.status == 'Filled' and session:
            real_exit      = trade.orderStatus.avgFillPrice or session['fill_price']
            buy_commission = session.get('commission', 1.0)
            log_trade_csv(ticker, session['fill_price'], real_exit, int(qty), reason, entry_dt,
                          commission=buy_commission)

    log("All positions submitted for closure.")


async def check_for_stopouts(ctx: AppContext, risk: RiskGuard, prev_positions: dict, watchlist: list) -> dict:
    current_positions = {p.contract.symbol: p.position
                         for p in await ctx.ib.reqPositionsAsync()}

    for ticker in watchlist:
        if prev_positions.get(ticker, 0) > 0 and current_positions.get(ticker, 0) == 0:
            log(f"  {ticker}: position gone — likely stopped out. Entering cooldown.")
            session = risk.session_trades.get(ticker)
            if session:
                entry_dt       = datetime.datetime.fromisoformat(session.get('entry_dt', datetime.datetime.now().isoformat()))
                # Use real fill price captured by execDetailsEvent hook in main.py
                # Falls back to SL price only if hook hasn't fired yet
                exit_price     = session.get('last_exit_price', session['sl'])
                buy_commission = session.get('commission', 1.0)
                log_trade_csv(ticker, session['fill_price'], exit_price,
                              session['qty'], "SL hit", entry_dt,
                              commission=buy_commission)
                risk.close_session_trade(ticker)
            risk.enter_cooldown(ticker)
            emit_event("STOPOUT", {"ticker": ticker})
    return current_positions


async def trade_rebalance(ctx: AppContext, strategy: dict, risk: RiskGuard, cfg: dict, reasoning: list = None):
    """
    Main trading loop — called every cycle with Grok's allocation dict.

    strategy: {ticker: pct_of_budget} — what Grok wants us to hold
    risk:     RiskGuard instance — tracks session trades, deployed cash, halt state
    cfg:      live config from config.json

    Logic per ticker:
      - If we hold more than target → trim (if significantly over)
      - If we hold less than target → buy the delta
      - If we're at target → just manage trailing SL
      - Pre-existing positions (not bought this session) → attach SL+TP if missing
    """
    if not is_market_open():
        log("Market closed — skipping.", "warning")
        return
    if within_30min_of_open():
        # First 30 min after open is highly volatile — skip to avoid bad fills.
        # TO CHANGE: edit config.json → timing.skip_first_minutes (requires helpers.py change too)
        log("Within 30 min of open — skipping.", "warning")
        return

    positions     = {p.contract.symbol: p.position
                     for p in await ctx.ib.reqPositionsAsync()}
    total_capital = await get_total_capital(ctx)

    ok, reason = risk.check_halt(total_capital)
    if not ok:
        log(f"Trading halted: {reason}", "warning")
        emit_event("HALT", {"reason": reason})
        return

    available_cash = await get_available_cash(ctx)
    cash_buffer    = cfg["risk"]["cash_buffer_pct"]
    cap_usd        = cfg["risk"].get("portfolio_cap_usd")
    spendable      = available_cash * (1.0 - cash_buffer)
    if cap_usd:
        already_in_market = total_capital - available_cash
        room_under_cap    = max(0.0, cap_usd - already_in_market)
        investable        = min(spendable, room_under_cap)
    else:
        investable = spendable
    investable = max(0.0, investable)
    log(f"Equity: ${total_capital:,.2f} | Cash: ${available_cash:,.2f} | "
        f"Deployed: ${risk.cash_deployed:.2f} | Free budget: ${investable:,.2f}")
    risk.log_session_summary()

    # Snapshot open orders
    open_buys:       dict[str, list] = {}
    open_protection: dict[str, dict] = {}

    for t in await ctx.ib.reqAllOpenOrdersAsync():
        sym    = t.contract.symbol
        action = t.order.action.upper()
        otype  = t.order.orderType.upper()
        status = t.orderStatus.status

        if sym not in cfg["watchlist"]:
            continue
        if status in ('Cancelled', 'Filled', 'Inactive'):
            continue

        if action == 'BUY':
            open_buys.setdefault(sym, []).append(t)
        elif action == 'SELL' and otype in ('STP', 'LMT'):
            open_protection.setdefault(sym, {})[otype] = t

    held_tickers = {sym for sym, qty in positions.items() if qty > 0 and sym in cfg["watchlist"]}
    all_tickers  = {t: strategy.get(t, 0) for t in (set(strategy.keys()) | held_tickers)}

    # News-driven SL adjustment — scan fresh news for all held positions every cycle.
    # Tightens SL if sentiment has turned bearish. TP is never touched.
    # TO DISABLE: remove this block and set sl_adjustments = {}
    # TO CHANGE sensitivity: edit the Rules section in get_sl_adjustments() prompt in strategy.py
    # TO CHANGE minimum SL distance: edit the 0.98 multiplier below (default 2% away from price)
    sl_adjustments: dict[str, float] = {}
    if held_tickers:
        held_info = {}
        for sym in held_tickers:
            session = risk.session_trades.get(sym)
            if not session:
                continue
            contract = Stock(sym, 'SMART', 'USD')
            await ctx.ib.qualifyContractsAsync(contract)
            ctx.ib.reqMarketDataType(1)
            [td] = await ctx.ib.reqTickersAsync(contract)
            price = td.marketPrice() or td.last or td.close
            if price and price > 0:
                held_info[sym] = {
                    "qty":           session["qty"],
                    "entry":         session["fill_price"],
                    "current_price": price,
                    "sl":            session["sl"],
                    "tp":            session.get("tp", 0),
                }
        if held_info:
            sl_adjustments = await get_sl_adjustments(ctx, held_info, cfg)

    for ticker, pct in all_tickers.items():
        if ticker not in cfg["watchlist"]:
            continue
        if risk.in_cooldown(ticker):
            log(f"  {ticker}: in cooldown — skip.")
            continue
        if ticker in open_buys:
            log(f"  {ticker}: BUY order already pending — not adding more.")
            continue

        contract = Stock(ticker, 'SMART', 'USD')
        await ctx.ib.qualifyContractsAsync(contract)

        ctx.ib.reqMarketDataType(1)
        [ticker_data] = await ctx.ib.reqTickersAsync(contract)
        price = ticker_data.marketPrice() or ticker_data.last or ticker_data.close

        if not price or price != price or price <= 0:
            log(f"  SKIP {ticker}: bad price.", "warning")
            continue

        atr = await get_atr(ctx, contract, cfg)
        if atr is None:
            log(f"  SKIP {ticker}: no ATR.", "warning")
            continue

        sl_distance = round(atr * cfg["atr"]["sl_multiplier"], 2)
        tp_distance = round(atr * cfg["atr"]["tp_multiplier"], 2)

        current_qty    = int(positions.get(ticker, 0))
        in_session     = ticker in risk.session_trades
        is_preexisting = current_qty > 0 and not in_session


        # ---- Pre-existing position protection ----
        if is_preexisting:
            log(f"{ticker} | held={current_qty}sh | pre-existing — managing full position protection.")
            min_sl = round(price * (1 - cfg["atr"]["sl_multiplier"] * 0.03), 2)

            existing_stp = open_protection.get(ticker, {}).get('STP')
            existing_lmt = open_protection.get(ticker, {}).get('LMT')

            if existing_stp:
                current_sl  = existing_stp.order.auxPrice
                trailing_sl = round(price - sl_distance, 2)
                trailing_sl = max(trailing_sl, min_sl)
                if trailing_sl > current_sl * (1 + cfg["atr"]["sl_update_min_pct"]):
                    log(f"  {ticker}: trailing stop ${current_sl:.2f} → ${trailing_sl:.2f} (all {current_qty}sh)")
                    ctx.ib.cancelOrder(existing_stp.order)
                    await asyncio.sleep(0.3)
                    new_sl          = StopOrder('SELL', current_qty, trailing_sl, account=ctx.ib_acc, tif='GTC')
                    new_sl.transmit = True
                    ctx.ib.placeOrder(contract, new_sl)
                else:
                    log(f"  {ticker}: stop ${current_sl:.2f} still valid — not lowering.")
            else:
                sl_price = round(price - sl_distance, 2)
                sl_price = max(sl_price, min_sl)
                log(f"  {ticker}: no SL found — placing SL=${sl_price} for all {current_qty}sh")
                new_sl          = StopOrder('SELL', current_qty, sl_price, account=ctx.ib_acc, tif='GTC')
                new_sl.transmit = True
                ctx.ib.placeOrder(contract, new_sl)

            if not existing_lmt and existing_stp:
                tp_price  = round(price + tp_distance, 2)
                oca_group = f"OCA_{ticker}_{int(asyncio.get_event_loop().time())}"
                log(f"  {ticker}: no TP found — placing TP=${tp_price} for all {current_qty}sh (OCA with SL)")
                ctx.ib.cancelOrder(existing_stp.order)
                await asyncio.sleep(0.3)
                new_sl          = StopOrder('SELL', current_qty, existing_stp.order.auxPrice, account=ctx.ib_acc, tif='GTC')
                new_sl.ocaGroup = oca_group
                new_sl.ocaType  = 1
                new_sl.transmit = True
                new_tp          = LimitOrder('SELL', current_qty, tp_price, account=ctx.ib_acc, tif='GTC')
                new_tp.ocaGroup = oca_group
                new_tp.ocaType  = 1
                new_tp.transmit = True
                ctx.ib.placeOrder(contract, new_sl)
                ctx.ib.placeOrder(contract, new_tp)
            elif not existing_lmt and not existing_stp:
                log(f"  {ticker}: no SL yet — TP will be placed next cycle once SL is confirmed.")
            else:
                log(f"  {ticker}: TP=${existing_lmt.order.lmtPrice} already set — leaving untouched.")

        # ---- Delta calculation ----
        session_qty = risk.session_trades[ticker]['qty'] if in_session else 0

        if investable <= 0:
            # No free cash — hold everything, don't trim
            target_qty = session_qty
        elif pct <= 0 and in_session:
            # Grok returned 0% for a held position. This means "no new buy signal",
            # NOT "sell". We hold by default. Only sell if Grok explicitly flags an
            # exit (future: position_updates with action="exit").
            target_qty = session_qty
            log(f"  {ticker}: Grok has no buy signal but we hold — keeping {session_qty}sh.")
        else:
            target_value = (pct / 100) * investable
            target_qty   = int(target_value / price)
            # Never trim below what we originally bought this session.
            # The free budget shrinks as we buy more tickers, which would make
            # earlier positions look "over target" and trigger spurious trims.
            # SL/TP protect the downside — let them do their job.
            if in_session:
                target_qty = max(target_qty, session_qty)
        delta_qty = target_qty - session_qty

        has_sl = 'STP' in open_protection.get(ticker, {})
        has_tp = 'LMT' in open_protection.get(ticker, {})

        log(f"{ticker} | ${price:.2f} | ATR=${atr:.2f} | "
            f"held={current_qty}sh | target={target_qty}sh | delta={delta_qty:+d}sh | "
            f"SL={'✓' if has_sl else '✗'} TP={'✓' if has_tp else '✗'}")

        # ---- CASE 1: At or above target ----
        if delta_qty == 0 or (delta_qty < 0 and session_qty > 0):
            # Hard guard: never sell what we don't hold in IBKR
            if current_qty <= 0 and delta_qty < 0:
                log(f"  {ticker}: SELL blocked — IBKR shows 0 shares held (stale session_trades?).")
                risk.close_session_trade(ticker)
                continue
            if session_qty <= 0 and not is_preexisting:
                continue
            if session_qty <= 0:
                continue

            if delta_qty < 0:
                over_pct = abs(delta_qty) / target_qty if target_qty > 0 else 1
                # TO CHANGE trim sensitivity: adjust the 0.10 threshold (10% over target triggers trim)
                if over_pct < 0.10:
                    log(f"  {ticker}: {abs(delta_qty)}sh over target but within 10% — not trimming.")
                    delta_qty = 0
                else:
                    ok, reason = risk.check_halt(total_capital)
                    if not ok:
                        log(f"  Stopping: {reason}", "warning")
                        break
                    # Use current_qty (IBKR real) not session_qty (bot memory)
                    # Prevents selling more than we actually hold → short sell rejection
                    if current_qty <= 0:
                        log(f"  {ticker}: trim skipped — IBKR shows 0 shares held.")
                        continue
                    trim_qty = min(abs(delta_qty), current_qty)
                    remaining_qty = current_qty - trim_qty
                    log(f"  TRIM {trim_qty} x {ticker} (significantly over target, {remaining_qty}sh remaining)")

                    if ticker in open_protection:
                        for pt in open_protection[ticker].values():
                            ctx.ib.cancelOrder(pt.order)
                        await asyncio.sleep(0.5)

                    sell_order          = MarketOrder('SELL', trim_qty, account=ctx.ib_acc)
                    sell_order.tif      = 'DAY'
                    sell_order.transmit = True
                    ctx.ib.placeOrder(contract, sell_order)
                    risk.record_day_trade()
                    log_order("SELL (trim)", ticker, trim_qty, price, reason=reasoning)
                    emit_event("TRIM", {"ticker": ticker, "qty": trim_qty, "price": price})

                    if remaining_qty > 0:
                        sl_price  = round(price - sl_distance, 2)
                        tp_price  = round(price + tp_distance, 2)
                        oca_group = f"OCA_{ticker}_{int(asyncio.get_event_loop().time())}"
                        sl = StopOrder('SELL', remaining_qty, sl_price, account=ctx.ib_acc, tif='GTC')
                        tp = LimitOrder('SELL', remaining_qty, tp_price, account=ctx.ib_acc, tif='GTC')
                        sl.ocaGroup = oca_group
                        sl.ocaType  = 1
                        tp.ocaGroup = oca_group
                        tp.ocaType  = 1
                        sl.transmit = True
                        tp.transmit = True
                        ctx.ib.placeOrder(contract, sl)
                        ctx.ib.placeOrder(contract, tp)
                    continue

            protected_qty = current_qty if current_qty > 0 else session_qty

            if delta_qty == 0 and has_sl:
                existing_stp = open_protection[ticker]['STP']
                current_sl   = existing_stp.order.auxPrice
                trailing_sl  = round(price - sl_distance, 2)
                trailing_sl  = max(trailing_sl, round(price * 0.93, 2))

                if trailing_sl > current_sl * (1 + cfg["atr"]["sl_update_min_pct"]):
                    log(f"  {ticker}: trailing stop ${current_sl:.2f} → ${trailing_sl:.2f} ({protected_qty}sh)")
                    ctx.ib.cancelOrder(existing_stp.order)
                    await asyncio.sleep(0.3)
                    new_sl          = StopOrder('SELL', protected_qty, trailing_sl,
                                                account=ctx.ib_acc, tif='GTC')
                    new_sl.transmit = True
                    ctx.ib.placeOrder(contract, new_sl)
                else:
                    log(f"  {ticker}: stop ${current_sl:.2f} still valid — no update.")

            # News-driven SL tighten — overrides ATR trailing if bearish signal found
            if ticker in sl_adjustments and has_sl:
                news_sl      = round(sl_adjustments[ticker], 2)
                existing_stp = open_protection.get(ticker, {}).get('STP')
                current_sl   = existing_stp.order.auxPrice if existing_stp else 0
                min_distance = round(price * 0.98, 2)  # hard floor: SL must be ≥2% below price
                news_sl      = min(news_sl, min_distance)  # never closer than 1.5%
                # Only apply if tighter than current SL and safely below current price
                if news_sl > current_sl and news_sl < price:
                    log(f"  {ticker}: NEWS tightening SL ${current_sl:.2f} → ${news_sl:.2f} (min distance enforced)")
                    if existing_stp:
                        ctx.ib.cancelOrder(existing_stp.order)
                        await asyncio.sleep(0.3)
                    new_sl          = StopOrder('SELL', protected_qty, news_sl, account=ctx.ib_acc, tif='GTC')
                    new_sl.transmit = True
                    ctx.ib.placeOrder(contract, new_sl)
                    risk.session_trades[ticker]['sl'] = news_sl
                elif news_sl <= current_sl:
                    log(f"  {ticker}: news SL ${news_sl:.2f} not tighter than current ${current_sl:.2f} — keeping.")

            elif delta_qty == 0 and not has_sl:
                log(f"  {ticker}: position unprotected — attaching SL+TP now.")
                sl_price  = round(price - sl_distance, 2)
                tp_price  = round(price + tp_distance, 2)
                oca_group = f"OCA_{ticker}_{int(asyncio.get_event_loop().time())}"
                sl = StopOrder('SELL', protected_qty, sl_price, account=ctx.ib_acc, tif='GTC')
                tp = LimitOrder('SELL', protected_qty, tp_price, account=ctx.ib_acc, tif='GTC')
                sl.ocaGroup = oca_group
                sl.ocaType  = 1
                tp.ocaGroup = oca_group
                tp.ocaType  = 1
                sl.transmit = True
                tp.transmit = True
                ctx.ib.placeOrder(contract, sl)
                ctx.ib.placeOrder(contract, tp)

        # ---- CASE 2: Under target — buy ----
        elif delta_qty >= MIN_DELTA:
            ok, reason = risk.check_halt(total_capital)
            if not ok:
                log(f"  Stopping: {reason}", "warning")
                break

            log(f"  BUY {delta_qty} x {ticker}")
            buy_order          = MarketOrder('BUY', delta_qty, account=ctx.ib_acc)
            buy_order.tif      = 'DAY'
            buy_order.transmit = True
            trade = ctx.ib.placeOrder(contract, buy_order)

            deadline = asyncio.get_event_loop().time() + 30
            while not trade.isDone() and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.5)

            if not (trade.isDone() and trade.orderStatus.status == 'Filled'):
                log(f"  {ticker} BUY not filled in 30s — leaving unprotected for now.", "warning")
                continue

            fill_price     = trade.orderStatus.avgFillPrice or price
            new_session    = session_qty + delta_qty
            sl_price       = round(fill_price - sl_distance, 2)
            tp_price       = round(fill_price + tp_distance, 2)
            rr             = cfg["atr"]["tp_multiplier"] / cfg["atr"]["sl_multiplier"]
            # Grab commission from fill report (IBKR sends it with the fill)
            buy_commission = sum(f.commissionReport.commission for f in trade.fills
                                 if f.commissionReport.commission > 0) or 1.0

            log(f"  FILLED @ ${fill_price:.2f} | SL=${sl_price} TP=${tp_price} R:R=1:{rr:.1f} | commission=${buy_commission:.2f} | session={new_session}sh")
            risk.record_session_trade(ticker, new_session, fill_price, sl_price, tp_price)
            risk.session_trades[ticker]['commission'] = buy_commission
            # entry_dt stored in session_trades for CSV logging on close
            ticker_reason = [r for r in (reasoning or []) if ticker in r]
            log_order("BUY", ticker, delta_qty, fill_price, sl_price, tp_price, ticker_reason)
            emit_event("BUY", {"ticker": ticker, "qty": delta_qty, "price": fill_price,
                                "sl": sl_price, "tp": tp_price})

            if ticker in open_protection:
                for t in open_protection[ticker].values():
                    ctx.ib.cancelOrder(t.order)
                await asyncio.sleep(0.5)

            oca_group = f"OCA_{ticker}_{int(asyncio.get_event_loop().time())}"
            sl = StopOrder('SELL', new_session, sl_price, account=ctx.ib_acc, tif='GTC')
            tp = LimitOrder('SELL', new_session, tp_price, account=ctx.ib_acc, tif='GTC')
            sl.ocaGroup = oca_group
            sl.ocaType  = 1
            tp.ocaGroup = oca_group
            tp.ocaType  = 1
            sl.transmit = True
            tp.transmit = True
            ctx.ib.placeOrder(contract, sl)
            ctx.ib.placeOrder(contract, tp)

            if current_qty > 0:
                risk.record_day_trade()
