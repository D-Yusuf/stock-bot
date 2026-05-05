import os
import json
import datetime
from config import BOT_DIR
from logger import log

_SESSION_FILE = os.path.join(BOT_DIR, "session_trades.json")


def _load_session_trades() -> dict:
    """Load today's session trades from disk, discard if from a previous day."""
    if not os.path.exists(_SESSION_FILE):
        return {}
    try:
        with open(_SESSION_FILE) as f:
            data = json.load(f)
        if data.get("date") != str(datetime.date.today()):
            return {}
        return data.get("trades", {})
    except Exception:
        return {}


def _save_session_trades(trades: dict):
    with open(_SESSION_FILE, "w") as f:
        json.dump({"date": str(datetime.date.today()), "trades": trades}, f)


# TO CHANGE risk limits: edit config.json → risk (daily_loss_limit_pct, cash_buffer_pct, cooldown_cycles_after_stopout)
# All values are loaded from config.json — no code changes needed for tuning
class RiskGuard:
    def __init__(self, starting_equity: float, daily_loss_pct: float, cooldown_cycles: int):
        self.starting_equity = starting_equity
        self.daily_loss_pct  = daily_loss_pct
        self.loss_limit      = starting_equity * daily_loss_pct
        self.cooldown_cycles = cooldown_cycles
        self.halted          = False
        self.halt_reason     = ""
        self._cooldown: dict[str, int] = {}
        self.session_trades: dict[str, dict] = _load_session_trades()
        self.cash_deployed: float = 0.0  # don't count prior-session buys as locked cash on restart
        if self.session_trades:
            log(f"  Restored {len(self.session_trades)} session trade(s) from disk: "
                f"{list(self.session_trades.keys())}")

    def reset(self, new_equity: float):
        """Reset for a new trading day without creating a new object."""
        self.starting_equity = new_equity
        self.loss_limit      = new_equity * self.daily_loss_pct
        self.halted          = False
        self.halt_reason     = ""
        self._cooldown.clear()
        self.session_trades.clear()
        self.cash_deployed = 0.0
        _save_session_trades({})
        log(f"New day — risk guard reset. Equity: ${new_equity:,.2f}")

    def record_session_trade(self, ticker: str, qty: int, fill_price: float, sl: float, tp: float):
        old = self.session_trades.get(ticker, {})
        self.cash_deployed -= old.get('qty', 0) * old.get('fill_price', 0)
        self.session_trades[ticker] = {
            'qty': qty, 'fill_price': fill_price, 'sl': sl, 'tp': tp,
            'time': datetime.datetime.now().strftime('%H:%M:%S'),
            'entry_dt': datetime.datetime.now().isoformat(),
        }
        self.cash_deployed += qty * fill_price
        _save_session_trades(self.session_trades)
        log(f"  Session memory: {ticker} {qty}sh @ ${fill_price:.2f} | "
            f"SL=${sl} TP=${tp} | total deployed: ${self.cash_deployed:.2f}")

    def close_session_trade(self, ticker: str):
        t = self.session_trades.pop(ticker, {})
        self.cash_deployed -= t.get('qty', 0) * t.get('fill_price', 0)
        self.cash_deployed  = max(0.0, self.cash_deployed)
        _save_session_trades(self.session_trades)

    def log_session_summary(self):
        if not self.session_trades:
            log("  Session trades: none yet.")
            return
        log("  ── Session trades this session ──")
        for ticker, t in self.session_trades.items():
            log(f"  {ticker}: {t['qty']}sh @ ${t['fill_price']:.2f} "
                f"| SL=${t['sl']} TP=${t['tp']} | opened {t['time']}")
        log(f"  Total cash deployed: ${self.cash_deployed:.2f}")

    def tick_cooldowns(self):
        for ticker in list(self._cooldown):
            self._cooldown[ticker] -= 1
            if self._cooldown[ticker] <= 0:
                del self._cooldown[ticker]
                log(f"  Cooldown expired: {ticker} eligible to re-buy.")

    def enter_cooldown(self, ticker: str):
        self._cooldown[ticker] = self.cooldown_cycles
        log(f"  Cooldown started: {ticker} ({self.cooldown_cycles} cycles).")

    def in_cooldown(self, ticker: str) -> bool:
        return ticker in self._cooldown

    def record_day_trade(self):
        log(f"  Sale recorded. Note: proceeds settle T+1 (next business day).")

    def check_halt(self, current_equity: float) -> tuple[bool, str]:
        if self.halted:
            return False, self.halt_reason
        loss = self.starting_equity - current_equity
        if loss >= self.loss_limit:
            self.halted      = True
            self.halt_reason = (f"Daily loss limit hit: lost ${loss:.2f} "
                                f"(limit ${self.loss_limit:.2f})")
            log(f"RISK HALT: {self.halt_reason}", "warning")
            return False, self.halt_reason
        log(f"  Risk OK | loss so far: ${loss:.2f} | budget left: ${self.loss_limit - loss:.2f}")
        return True, ""
