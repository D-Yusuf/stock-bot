import os
import re
import logging
import datetime
from logging.handlers import RotatingFileHandler
from config import BOT_DIR

# ---------------------------------------------------------------------------
# Rotating file handler
# ---------------------------------------------------------------------------
_handler = RotatingFileHandler(
    os.path.join(BOT_DIR, 'bot_activity.log'),
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
)
_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
))
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_handler)

_order_log_path = os.path.join(BOT_DIR, "orders.log")


# ---------------------------------------------------------------------------
# Core log function
# ---------------------------------------------------------------------------
def log(msg, level="info"):
    print(msg)
    getattr(logging, level)(msg)


def sanitize(text):
    text = re.sub(r'[Uu]\d{4,10}', '[ACCOUNT_ID]', text)
    text = re.sub(r'\$?\d{1,3}(,\d{3})*(\.\d+)?', '[AMOUNT]', text)
    return text


# ---------------------------------------------------------------------------
# Order log — clean, human-readable, one entry per trade
# ---------------------------------------------------------------------------
def log_order(action: str, ticker: str, qty: int, price: float,
              sl: float = None, tp: float = None, reason: list = None):
    now   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append(f"{'─' * 60}")
    lines.append(f"  {now}  |  {action}  |  {ticker}  |  {qty} shares @ ${price:.2f}")
    if sl:
        lines.append(f"  Stop Loss : ${sl:.2f}")
    if tp:
        lines.append(f"  Take Profit: ${tp:.2f}")
    if sl and tp:
        rr = (tp - price) / (price - sl) if price != sl else 0
        lines.append(f"  R:R Ratio  : 1:{rr:.2f}")
    if reason:
        lines.append(f"  Analysis:")
        for r in (reason if isinstance(reason, list) else [reason]):
            lines.append(f"    • {r}")
    lines.append("")
    entry = "\n".join(lines)
    with open(_order_log_path, "a") as f:
        f.write(entry + "\n")
    print(entry)


# ---------------------------------------------------------------------------
# Trade CSV logger — one row per closed trade for performance analysis
# Opens in Excel/Sheets. Appended automatically, never overwritten.
# ---------------------------------------------------------------------------
_CSV_PATH = os.path.join(BOT_DIR, "progress", "trades.csv")
_CSV_HEADERS = "date,ticker,entry_price,exit_price,qty,pnl,exit_reason,hold_minutes\n"


def log_trade_csv(ticker: str, entry_price: float, exit_price: float,
                  qty: int, exit_reason: str, entry_time: datetime.datetime,
                  commission: float = 0.0):
    os.makedirs(os.path.dirname(_CSV_PATH), exist_ok=True)
    write_header = not os.path.exists(_CSV_PATH)
    hold_minutes = int((datetime.datetime.now() - entry_time).total_seconds() / 60)
    pnl          = round((exit_price - entry_price) * qty, 2)
    row = (f"{datetime.date.today()},{ticker},{entry_price:.2f},{exit_price:.2f},"
           f"{qty},{pnl:+.2f},{exit_reason},{hold_minutes}\n")
    with open(_CSV_PATH, "a") as f:
        if write_header:
            f.write(_CSV_HEADERS)
        f.write(row)
    log(f"  CSV logged: {ticker} {exit_reason} P&L={pnl:+.2f} hold={hold_minutes}min")


# ---------------------------------------------------------------------------
# Event bus — Telegram (or anything else) registers listeners here
# ---------------------------------------------------------------------------
_event_listeners: list = []


def register_event_listener(fn):
    _event_listeners.append(fn)


def emit_event(event_type: str, data: dict):
    for fn in _event_listeners:
        try:
            fn(event_type, data)
        except Exception:
            pass
