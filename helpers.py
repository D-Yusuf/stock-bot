import sys
import threading
import datetime
import zoneinfo
from logger import log

ET_TZ = zoneinfo.ZoneInfo("America/New_York")
KW_TZ = zoneinfo.ZoneInfo("Asia/Kuwait")

# Global shutdown flag — set by 'x' keypress listener
shutdown_requested = False


def is_market_open() -> bool:
    now = datetime.datetime.now(ET_TZ)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now < close_t


def within_30min_of_open() -> bool:
    # Skip first 10 min only — reduced from 30 to catch gap moves on news
    now    = datetime.datetime.now(ET_TZ)
    open_t = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    cutoff = now.replace(hour=9,  minute=40, second=0, microsecond=0)
    return open_t <= now < cutoff


def is_eod_close_window(minutes_before: int = 15) -> bool:
    """Returns True when within `minutes_before` minutes of market close (default 15 min)."""
    now     = datetime.datetime.now(ET_TZ)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    cutoff  = close_t - datetime.timedelta(minutes=minutes_before)
    return cutoff <= now < close_t


def is_friday_eod(minutes_before: int = 15) -> bool:
    """Returns True on Friday within `minutes_before` minutes of close — hard close day."""
    now = datetime.datetime.now(ET_TZ)
    return now.weekday() == 4 and is_eod_close_window(minutes_before)


def next_market_open_secs() -> int:
    now = datetime.datetime.now(ET_TZ)
    candidate = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if candidate <= now or now.weekday() >= 5:
        days_ahead = 1
        next_day = now + datetime.timedelta(days=days_ahead)
        while next_day.weekday() >= 5:
            days_ahead += 1
            next_day = now + datetime.timedelta(days=days_ahead)
        candidate = next_day.replace(hour=9, minute=30, second=0, microsecond=0)
    return int((candidate - now).total_seconds())


def kuwait_time_str() -> str:
    return datetime.datetime.now(KW_TZ).strftime("%H:%M:%S")


def _keyboard_listener():
    global shutdown_requested
    print("\n[Bot running] Press  x + Enter  at any time to safely close all positions and exit.\n")
    for line in sys.stdin:
        if line.strip().lower() == 'x':
            log("Shutdown key pressed. Finishing current cycle then closing all positions...")
            shutdown_requested = True
            break


def start_keyboard_listener():
    t = threading.Thread(target=_keyboard_listener, daemon=True)
    t.start()
