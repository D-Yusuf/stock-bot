import os
import re
import logging
import datetime
from logging.handlers import RotatingFileHandler
from config import BOT_DIR

# ---------------------------------------------------------------------------
# Noise filter — suppresses IBKR internal chatter from ib_async
# ---------------------------------------------------------------------------
_NOISE_PATTERNS = re.compile(
    r'updatePortfolio:|orderStatus:|position:|execDetails:|'
    r'commissionReport:|openOrder:|accountValue:|'
    r'Warning \d+, reqId -1:|'
    r'HTTP Request: (GET|POST) https://',
    re.IGNORECASE,
)


class _BotFilter(logging.Filter):
    def filter(self, record):
        return not _NOISE_PATTERNS.search(record.getMessage())


# ---------------------------------------------------------------------------
# Rotating file handler — bot messages only, 2 MB × 3 backups
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
_handler.addFilter(_BotFilter())

# Console handler — same filter, no timestamps (cleaner terminal output)
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter('%(message)s'))
_console.addFilter(_BotFilter())

root = logging.getLogger()
root.setLevel(logging.INFO)
# Remove any handlers added by ib_async or other libs before ours
root.handlers.clear()
root.addHandler(_handler)
# Don't add a second console handler — log() calls print() directly

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
# Trade CSV logger — one row per closed trade, deduped on write
# ---------------------------------------------------------------------------
_CSV_PATH    = os.path.join(BOT_DIR, "progress", "trades.csv")
_CSV_HEADERS = "date,ticker,entry_price,exit_price,qty,pnl,exit_reason,hold_minutes\n"


def log_trade_csv(ticker: str, entry_price: float, exit_price: float,
                  qty: int, exit_reason: str, entry_time: datetime.datetime,
                  commission: float = 0.0):
    os.makedirs(os.path.dirname(_CSV_PATH), exist_ok=True)
    hold_minutes = int((datetime.datetime.now() - entry_time).total_seconds() / 60)
    pnl          = round((exit_price - entry_price) * qty, 2)
    row = (f"{datetime.date.today()},{ticker},{entry_price:.2f},{exit_price:.2f},"
           f"{qty},{pnl:+.2f},{exit_reason},{hold_minutes}\n")

    # Read existing rows to avoid duplicates
    existing = set()
    if os.path.exists(_CSV_PATH):
        with open(_CSV_PATH) as f:
            for line in f:
                existing.add(line.strip())

    if row.strip() in existing:
        log(f"  CSV skip (duplicate): {ticker} {exit_reason}")
        return

    write_header = not os.path.exists(_CSV_PATH)
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
