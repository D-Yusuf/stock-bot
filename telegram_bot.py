import os
import json
import asyncio
import subprocess
import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import AppContext, BOT_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from logger import log, register_event_listener, sanitize
from risk import RiskGuard

PLIST_LABEL = "com.stockbot"
PLIST_PATH  = os.path.expanduser("~/Library/LaunchAgents/com.stockbot.plist")


class TelegramBot:
    def __init__(self, ctx: AppContext, risk: RiskGuard):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")

        self.ctx      = ctx
        self.risk     = risk
        self.chat_id  = int(TELEGRAM_CHAT_ID)
        self.app      = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self._register_handlers()

        # Register for push notifications from trading events
        register_event_listener(
            lambda etype, data: asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.ensure_future, self.push(etype, data)
            )
        )

    def _register_handlers(self):
        self.app.add_handler(CommandHandler("run", self._cmd_run))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("logs", self._cmd_logs))
        self.app.add_handler(CommandHandler("orders", self._cmd_orders))
        self.app.add_handler(CommandHandler("say", self._cmd_say))
        self.app.add_handler(CommandHandler("help", self._cmd_help))

    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        await self.app.bot.send_message(
            chat_id=self.chat_id,
            text="Stock Bot connected. Type /help for commands."
        )

    async def stop(self):
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Push notifications
    # ------------------------------------------------------------------
    async def push(self, event_type: str, data: dict):
        ticker = data.get("ticker", "")
        qty    = data.get("qty", "")
        price  = data.get("price", "")

        if event_type == "BUY":
            sl = data.get("sl", "")
            tp = data.get("tp", "")
            msg = f"🟢 BUY {qty}x {ticker} @ ${price:.2f}\nSL: ${sl:.2f} | TP: ${tp:.2f}"
        elif event_type == "SELL":
            msg = f"🔴 SELL {qty}x {ticker} — {data.get('reason', '')}"
        elif event_type == "TRIM":
            msg = f"✂️ TRIM {qty}x {ticker} @ ${price:.2f}"
        elif event_type == "STOPOUT":
            msg = f"⛔ STOP-OUT {ticker} — position gone, entering cooldown"
        elif event_type == "HALT":
            msg = f"🚨 TRADING HALTED — {data.get('reason', '')}"
        else:
            msg = f"[{event_type}] {data}"

        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=msg)
        except Exception as e:
            log(f"Telegram push failed: {e}", "error")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = (
            "/run — Start the bot service\n"
            "/stop — Stop the bot service\n"
            "/status — Current positions & P&L\n"
            "/logs — Last 20 lines of activity log\n"
            "/orders — Session trades\n"
            "/say <message> — Ask Grok a question\n"
        )
        await update.message.reply_text(msg)

    async def _cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        out = subprocess.run(
            ["launchctl", "load", PLIST_PATH],
            capture_output=True, text=True
        )
        msg = out.stdout + out.stderr if (out.stdout + out.stderr).strip() else "Bot service loaded."
        await update.message.reply_text(msg.strip())

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        out = subprocess.run(
            ["launchctl", "unload", PLIST_PATH],
            capture_output=True, text=True
        )
        msg = out.stdout + out.stderr if (out.stdout + out.stderr).strip() else "Bot service stopped."
        await update.message.reply_text(msg.strip())

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            positions = {p.contract.symbol: (p.position, p.avgCost)
                         for p in await self.ctx.ib.reqPositionsAsync()
                         if p.position > 0}
            if not positions:
                await update.message.reply_text("No open positions.")
                return

            lines = ["📊 Current Positions:\n"]
            for sym, (qty, avg) in sorted(positions.items()):
                lines.append(f"  {sym}: {qty:.0f}sh @ ${avg:.2f}")

            # Add session trade info
            if self.risk.session_trades:
                lines.append("\n📝 Session Trades:")
                for t, info in self.risk.session_trades.items():
                    lines.append(f"  {t}: {info['qty']}sh @ ${info['fill_price']:.2f} "
                                 f"| SL=${info['sl']} TP=${info['tp']}")
                lines.append(f"\n💰 Deployed: ${self.risk.cash_deployed:.0f}")

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        log_path = os.path.join(BOT_DIR, "bot_activity.log")
        try:
            with open(log_path) as f:
                lines = f.readlines()
            last = lines[-20:] if len(lines) > 20 else lines
            text = "".join(last)
            # Telegram has a 4096 char limit
            if len(text) > 4000:
                text = text[-4000:]
            await update.message.reply_text(f"📋 Last logs:\n\n{text}")
        except Exception as e:
            await update.message.reply_text(f"Error reading logs: {e}")

    async def _cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session_file = os.path.join(BOT_DIR, "session_trades.json")
        try:
            with open(session_file) as f:
                data = json.load(f)
            trades = data.get("trades", {})
            date   = data.get("date", "")

            if not trades:
                await update.message.reply_text(f"No session trades for {date}.")
                return

            lines = [f"📋 Session Trades ({date}):\n"]
            for ticker, t in trades.items():
                lines.append(
                    f"  {ticker}: {t['qty']}sh @ ${t['fill_price']:.2f}\n"
                    f"    SL=${t['sl']:.2f} | TP=${t['tp']:.2f} | {t.get('time', '')}"
                )
            await update.message.reply_text("\n".join(lines))
        except FileNotFoundError:
            await update.message.reply_text("No session trades file found.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_say(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Usage: /say <your message to Grok>")
            return

        msg = " ".join(context.args)

        # Write to bot_instructions.txt so the bot picks it up next cycle
        instructions_path = os.path.join(BOT_DIR, "bot_instructions.txt")
        with open(instructions_path, "w") as f:
            f.write(msg)

        await update.message.reply_text(f"✅ Instruction saved. Asking Grok...")

        # Ask Grok directly and reply
        try:
            from config import load_config
            cfg = load_config()
            response = self.ctx.client.responses.create(
                model=cfg["grok"]["model"],
                input=[{"role": "user", "content": sanitize(msg)}],
            )
            answer = response.output_text
            # Telegram 4096 char limit
            if len(answer) > 4000:
                answer = answer[:4000] + "..."
            await update.message.reply_text(f"🤖 Grok:\n\n{answer}")
        except Exception as e:
            await update.message.reply_text(f"Grok error: {e}")
