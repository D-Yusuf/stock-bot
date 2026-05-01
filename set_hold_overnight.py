"""
Standalone script — run this manually to ask Claude which current positions to hold overnight.
Updates session_trades.json so the main bot respects the flags at EOD close.

Usage:
  python set_hold_overnight.py
"""
import asyncio
import json
import os
from config import IB_HOST, IB_PORT, load_config, create_context, BOT_DIR
from logger import log
from strategy import get_hold_overnight_flags
from risk import RiskGuard


async def main():
    cfg = load_config()
    ctx = create_context()

    log("Connecting to IBKR...")
    await ctx.ib.connectAsync(IB_HOST, IB_PORT, clientId=16, timeout=60)
    log("Connected.")
    await asyncio.sleep(2)  # wait for portfolio to sync

    try:
        # Load existing session_trades
        trades_path = os.path.join(BOT_DIR, "session_trades.json")
        session_trades = {}
        if os.path.exists(trades_path):
            with open(trades_path) as f:
                session_trades = json.load(f)

        trades = session_trades.get("trades", session_trades)
        if not trades:
            log("No session trades found — nothing to evaluate.")
            return

        # Get current positions from IBKR
        positions = {p.contract.symbol: p.position
                     for p in await ctx.ib.reqPositionsAsync()}

        current_positions = {}
        for ticker, session in trades.items():
            qty = positions.get(ticker, 0)
            if qty > 0:
                current_positions[ticker] = {
                    "qty":   session["qty"],
                    "entry": session["fill_price"],
                    "price": session["fill_price"],
                    "pnl":   0.0,
                }

        if not current_positions:
            log("No held positions found in IBKR.")
            return

        log(f"Evaluating {len(current_positions)} positions for hold_overnight...")
        flags = await get_hold_overnight_flags(ctx, current_positions, cfg)

        # Write flags back into session_trades.json
        for ticker, flag in flags.items():
            if ticker in trades:
                trades[ticker]['hold_overnight'] = flag

        with open(trades_path, 'w') as f:
            json.dump(session_trades, f, indent=2)

        log("session_trades.json updated with hold_overnight flags.")
        log("Bot will respect these flags at EOD close.")

    finally:
        ctx.ib.disconnect()
        log("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
