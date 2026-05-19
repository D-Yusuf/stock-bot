"""
Stock Bot Dashboard — quick controls for the launchd service.
Run with:  python3 dashboard.py
"""
import tkinter as tk
from tkinter import ttk, scrolledtext
import subprocess
import threading
import json
import os
import sys

PLIST_LABEL      = "com.stockbot"
PLIST_PATH       = os.path.expanduser("~/Library/LaunchAgents/com.stockbot.plist")
BOT_DIR          = os.path.expanduser("~/stock-bot")
ACTIVITY_LOG     = os.path.join(BOT_DIR, "bot_activity.log")
SESSION_TRADES   = os.path.join(BOT_DIR, "session_trades.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return (result.stdout + result.stderr).strip()

def _bot_running() -> bool:
    out = _run(["launchctl", "list", PLIST_LABEL])
    return '"PID"' in out

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class Dashboard:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Stock Bot")
        self.root.geometry("820x580")
        self.root.resizable(True, True)

        self._log_pos = 0
        self._build_ui()
        self._start_log_tail()
        self._refresh_status()
        self._refresh_trades()

    # ---- UI ----------------------------------------------------------------

    def _build_ui(self):
        # Top bar — buttons + status
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Button(top, text="▶  Run",   width=12, command=self._cmd_run).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="■  Stop",  width=12, command=self._cmd_stop).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="?  Check", width=12, command=self._cmd_check).pack(side=tk.LEFT, padx=4)

        self.status_var = tk.StringVar(value="…")
        self.status_lbl = ttk.Label(top, textvariable=self.status_var, font=("Helvetica", 13, "bold"))
        self.status_lbl.pack(side=tk.RIGHT, padx=10)

        ttk.Separator(self.root, orient="horizontal").pack(fill=tk.X, padx=10)

        # Tabs
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        # Tab 1 — Activity log
        log_frame = ttk.Frame(nb, padding=4)
        nb.add(log_frame, text="  Activity Log  ")

        self.log_box = scrolledtext.ScrolledText(
            log_frame, state="disabled",
            font=("Courier", 11), bg="#0d0d0d", fg="#e0e0e0",
            insertbackground="white", wrap=tk.WORD,
        )
        self.log_box.pack(fill=tk.BOTH, expand=True)
        self.log_box.tag_config("error",  foreground="#ff5555")
        self.log_box.tag_config("buy",    foreground="#50fa7b")
        self.log_box.tag_config("sell",   foreground="#ffb86c")
        self.log_box.tag_config("cycle",  foreground="#8be9fd")
        self.log_box.tag_config("normal", foreground="#e0e0e0")

        # Tab 2 — Session trades
        trades_frame = ttk.Frame(nb, padding=4)
        nb.add(trades_frame, text="  Session Trades  ")

        # Treeview table
        cols = ("Ticker", "Qty", "Fill Price", "Stop Loss", "Take Profit", "Time")
        self.tree = ttk.Treeview(trades_frame, columns=cols, show="headings", height=12)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, anchor=tk.CENTER, width=110)
        self.tree.pack(fill=tk.BOTH, expand=True)

        ttk.Button(trades_frame, text="⟳  Refresh", command=self._refresh_trades).pack(pady=4)

    # ---- Button commands ---------------------------------------------------

    def _cmd_run(self):
        self._append("--- Loading bot service...\n", "cycle")
        threading.Thread(target=self._do_run, daemon=True).start()

    def _do_run(self):
        if _bot_running():
            self._append("Bot is already running.\n", "normal")
            self._refresh_status()
            return
        out = _run(["launchctl", "load", PLIST_PATH])
        msg = out if out else "Service loaded. Bot starting…"
        self._append(f"{msg}\n", "normal")
        import time; time.sleep(2)
        self._refresh_status()

    def _cmd_stop(self):
        self._append("--- Stopping bot service...\n", "sell")
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _do_stop(self):
        out = _run(["launchctl", "unload", PLIST_PATH])
        msg = out if out else "Service unloaded. Bot stopped."
        self._append(f"{msg}\n", "normal")
        self._refresh_status()

    def _cmd_check(self):
        threading.Thread(target=self._do_check, daemon=True).start()

    def _do_check(self):
        out = _run(["launchctl", "list", PLIST_LABEL])
        self._append(f"--- launchctl list ---\n{out}\n", "cycle")
        self._refresh_status()

    # ---- Status ------------------------------------------------------------

    def _refresh_status(self):
        running = _bot_running()
        if running:
            self.status_var.set("● RUNNING")
            self.status_lbl.config(foreground="#50fa7b")
        else:
            self.status_var.set("● STOPPED")
            self.status_lbl.config(foreground="#ff5555")
        self.root.after(5000, self._refresh_status)

    # ---- Activity log tail -------------------------------------------------

    def _start_log_tail(self):
        if not os.path.exists(ACTIVITY_LOG):
            open(ACTIVITY_LOG, "w").close()
        with open(ACTIVITY_LOG) as f:
            lines = f.readlines()
            last = lines[-80:] if len(lines) > 80 else lines
            for line in last:
                self._append(line, self._tag_for(line))
            self._log_pos = f.tell()
        self._poll_log()

    def _poll_log(self):
        try:
            with open(ACTIVITY_LOG) as f:
                f.seek(self._log_pos)
                new = f.read()
                if new:
                    self._log_pos = f.tell()
                    for line in new.splitlines(keepends=True):
                        self._append(line, self._tag_for(line))
        except Exception:
            pass
        self.root.after(1000, self._poll_log)

    @staticmethod
    def _tag_for(line: str) -> str:
        l = line.upper()
        if "ERROR" in l or "FATAL" in l:
            return "error"
        if "BUY" in l or "FILLED" in l:
            return "buy"
        if "SELL" in l or "STOP" in l or "CLOSE" in l:
            return "sell"
        if "CYCLE" in l or "===" in l:
            return "cycle"
        return "normal"

    def _append(self, text: str, tag: str = "normal"):
        def _do():
            self.log_box.configure(state="normal")
            self.log_box.insert(tk.END, text, tag)
            self.log_box.see(tk.END)
            self.log_box.configure(state="disabled")
        self.root.after(0, _do)

    # ---- Session trades ----------------------------------------------------

    def _refresh_trades(self):
        # Clear existing rows
        for row in self.tree.get_children():
            self.tree.delete(row)

        if not os.path.exists(SESSION_TRADES):
            return

        try:
            with open(SESSION_TRADES) as f:
                data = json.load(f)
            trades = data.get("trades", {})
            date   = data.get("date", "")

            if not trades:
                self.tree.insert("", tk.END, values=("No trades yet", "", "", "", "", ""))
                return

            for ticker, t in trades.items():
                self.tree.insert("", tk.END, values=(
                    ticker,
                    t.get("qty", ""),
                    f"${t.get('fill_price', 0):.2f}",
                    f"${t.get('sl', 0):.2f}",
                    f"${t.get('tp', 0):.2f}",
                    t.get("time", ""),
                ))
        except Exception as e:
            self.tree.insert("", tk.END, values=(f"Error: {e}", "", "", "", "", ""))

        # Auto-refresh every 10 seconds
        self.root.after(10000, self._refresh_trades)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()
    Dashboard(root)
    root.mainloop()
