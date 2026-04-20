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
