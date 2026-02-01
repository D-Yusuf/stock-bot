import time
import os

# ANSI Colors
CYAN = '\033[96m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
RESET = '\033[0m'
BOLD = '\033[1m'

LOG_FILE = "bot_activity.log"

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def print_header():
    print(f"{BOLD}{CYAN}========================================{RESET}")
    print(f"{BOLD}{CYAN}      STOCK BOT ACTIVITY MONITOR        {RESET}")
    print(f"{BOLD}{CYAN}========================================{RESET}")
    print(f"{YELLOW}Waiting for bot activity... (Ctrl+C to quit){RESET}")
    print("")

def monitor_log():
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'w').close()

    with open(LOG_FILE, 'r') as f:
        # Go to the end of file
        f.seek(0, 2)
        
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            
            # Format output based on content
            if "ERROR" in line:
                print(f"{RED}{line.strip()}{RESET}")
            elif "ORDER SENT" in line:
                print(f"{GREEN}{BOLD}{line.strip()}{RESET}")
            elif "GROK REASONING" in line:
                print(f"\n{CYAN}{BOLD}--- GROK INSIGHTS ---{RESET}")
                # Print the reasoning text wrapped nicely
                content = line.split("GROK REASONING:", 1)[1].strip()
                print(f"{CYAN}{content}{RESET}\n")
            else:
                print(f"{line.strip()}")

if __name__ == "__main__":
    try:
        clear_screen()
        print_header()
        monitor_log()
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
