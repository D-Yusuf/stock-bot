import asyncio
import datetime
from config import IB_HOST, IB_PORT, load_config, create_context
from logger import log
from helpers import (
    is_market_open, shutdown_requested, start_keyboard_listener,
    next_market_open_secs, kuwait_time_str, is_eod_close_window, is_friday_eod,
)
from account import get_total_capital
from risk import RiskGuard
from strategy import get_grok_strategy
from trading import trade_rebalance, close_all_positions, check_for_stopouts


async def main():
    import helpers  # for mutating shutdown_requested

    ctx = create_context()
    start_keyboard_listener()

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

    prev_positions = {p.contract.symbol: p.position
                      for p in await ctx.ib.reqPositionsAsync()}

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
                await close_all_positions(ctx, cfg["watchlist"], "user pressed x")
                break

            cfg        = load_config()
            watchlist  = cfg["watchlist"]
            cycle_secs = cfg["timing"]["cycle_seconds"]

            cycle_start = datetime.datetime.now()
            log(f"\n{'=' * 60}")
            log(f"CYCLE  {cycle_start:%Y-%m-%d %H:%M:%S}")
            log(f"{'=' * 60}")

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

                eod_cfg     = cfg.get("eod", {})
                close_mins  = eod_cfg.get("close_minutes_before", 15)

                # EOD close — 15 min before market close on any day
                if eod_cfg.get("close_all_eod") and is_eod_close_window(close_mins):
                    log("  EOD window — closing all positions before market close.")
                    await close_all_positions(ctx, watchlist, "end of day")
                    risk.reset(await get_total_capital(ctx))
                    log(f"Cycle done. Sleeping {cfg['timing']['cycle_seconds']}s...")
                    await asyncio.sleep(cfg["timing"]["cycle_seconds"])
                    continue

                # Friday hard close — no weekend risk
                if eod_cfg.get("close_all_friday") and is_friday_eod(close_mins):
                    log("  Friday EOD — closing all positions, no weekend holds.")
                    await close_all_positions(ctx, watchlist, "Friday close")
                    risk.reset(await get_total_capital(ctx))
                    log(f"Cycle done. Sleeping {cfg['timing']['cycle_seconds']}s...")
                    await asyncio.sleep(cfg["timing"]["cycle_seconds"])
                    continue

                available_cash = await get_available_cash(ctx)
                cap_usd        = cfg["risk"].get("portfolio_cap_usd")
                max_deploy     = cfg["risk"].get("max_deploy_pct", 0.70)

                # Apply 70% deploy cap and portfolio USD cap
                investable = available_cash * (1.0 - cfg["risk"]["cash_buffer_pct"])
                investable = investable * max_deploy
                if cap_usd:
                    investable = min(investable, cap_usd)

                free_budget = max(0.0, investable - risk.cash_deployed)
                log(f"  Budget: ${investable:.0f} cap ({int(max_deploy*100)}% deploy) | "
                    f"${risk.cash_deployed:.0f} deployed | ${free_budget:.0f} free")

                MIN_BUY_BUDGET = 150.0
                scan_budget = free_budget if free_budget >= MIN_BUY_BUDGET else 0.0

                if scan_budget == 0.0:
                    log("  No free cash to buy — running Grok in analysis-only mode (manage existing positions).")

                raw = await get_grok_strategy(ctx, watchlist, conversation, cfg, scan_budget)

                if raw:
                    reasoning = raw.pop("_reasoning", [])
                    await trade_rebalance(ctx, raw, risk, cfg, reasoning)
                else:
                    log("  No actionable signal.")
                    await trade_rebalance(ctx, {}, risk, cfg, [])

            log(f"Cycle done. Sleeping {cycle_secs}s...")
            await asyncio.sleep(cycle_secs)

    except KeyboardInterrupt:
        log("Ctrl-C received.")
        await close_all_positions(ctx, cfg["watchlist"], "Ctrl-C")
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
