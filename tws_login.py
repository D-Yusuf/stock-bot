"""
TWS Auto-Login — launches Trader Workstation and types credentials.
Run standalone or called from dashboard.py.
"""
import os
import time
import subprocess
import socket
from dotenv import load_dotenv

load_dotenv()

TWS_APP      = "/Users/mac/Applications/Trader Workstation/Trader Workstation.app"
TWS_USERNAME = os.getenv("TWS_USERNAME", "")
TWS_PASSWORD = os.getenv("TWS_PASSWORD", "")
IB_HOST      = os.getenv("IBKR_HOST", "127.0.0.1")
IB_PORT      = int(os.getenv("IBKR_PORT_LIVE", 7496))


def port_open() -> bool:
    try:
        s = socket.create_connection((IB_HOST, IB_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def login(status_cb=None):
    """
    status_cb: optional callback(str) for progress messages (used by dashboard).
    Returns True if API port opened successfully, False otherwise.
    """
    def log(msg):
        print(msg)
        if status_cb:
            status_cb(msg)

    if port_open():
        log("TWS already running and API port is open.")
        return True

    if not TWS_USERNAME or not TWS_PASSWORD:
        log("TWS_USERNAME/TWS_PASSWORD not set in .env — cannot auto-login.")
        return False

    # Launch TWS if not running
    try:
        out = subprocess.check_output(["pgrep", "-f", "Trader Workstation"], text=True)
        already_running = bool(out.strip())
    except subprocess.CalledProcessError:
        already_running = False

    if not already_running:
        log("Launching Trader Workstation...")
        subprocess.Popen(["open", "-a", TWS_APP])
        log("Waiting 15s for login window...")
        time.sleep(15)
    else:
        log("TWS running but not logged in — attempting login...")
        time.sleep(3)

    try:
        import pyautogui

        # Bring TWS to front
        subprocess.run(
            ["osascript", "-e", 'tell application "Trader Workstation" to activate'],
            check=False
        )
        time.sleep(8)

        # Use screen size to calculate positions — TWS login window is always
        # on the right side of the screen, roughly centered vertically
        import pyperclip

        # Username field is focused by default when TWS opens — paste directly
        log("Entering username...")
        pyperclip.copy(TWS_USERNAME)
        pyautogui.hotkey("command", "v")
        time.sleep(0.3)

        # Tab to password field
        pyautogui.press("tab")
        time.sleep(0.3)

        log("Entering password...")
        pyperclip.copy(TWS_PASSWORD)
        pyautogui.hotkey("command", "v")
        time.sleep(0.3)

        # Tab twice to reach Log In button, then space to click it
        log("Clicking Log In...")
        pyautogui.press("tab")
        time.sleep(0.2)
        pyautogui.press("tab")
        time.sleep(0.2)
        pyautogui.press("space")
        log("Credentials submitted — approve on your phone if prompted.")

    except Exception as e:
        log(f"Auto-login error: {e} — please log in manually.")
        return False

    # Wait up to 5 min for phone approval + API port
    log("Waiting up to 5 min for TWS API port to open...")
    deadline = time.time() + 300
    while time.time() < deadline:
        if port_open():
            log("TWS API port is open — ready.")
            return True
        time.sleep(3)

    log("Timed out waiting for TWS API port. Check TWS manually.")
    return False


if __name__ == "__main__":
    import sys
    ok = login()
    sys.exit(0 if ok else 1)
