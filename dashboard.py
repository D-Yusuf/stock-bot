import tkinter as tk
from tkinter import scrolledtext, ttk
import sys
import threading
import asyncio
import io
import datetime
from main import client, get_grok_strategy, trade_rebalance, ib, IB_PORT, sanitize_text

# Redirect stdout to the text widget
class TextRedirector(io.StringIO):
    def __init__(self, widget):
        self.widget = widget

    def write(self, str):
        self.widget.configure(state='normal')
        self.widget.insert(tk.END, str)
        self.widget.see(tk.END)
        self.widget.configure(state='disabled')

    def flush(self):
        pass

class TradingDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Stock Bot Dashboard")
        self.root.geometry("800x600")

        # Layout
        self.main_frame = ttk.Frame(root, padding="10")
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. Control Panel
        self.controls_frame = ttk.LabelFrame(self.main_frame, text="Controls", padding="10")
        self.controls_frame.pack(fill=tk.X, pady=5)

        self.btn_run = ttk.Button(self.controls_frame, text="RUN STRATEGY", command=self.run_strategy_thread)
        self.btn_run.pack(side=tk.LEFT, padx=5)

        self.status_lbl = ttk.Label(self.controls_frame, text="Status: Ready")
        self.status_lbl.pack(side=tk.LEFT, padx=20)

        # 2. Console / Reasoning Output
        self.log_frame = ttk.LabelFrame(self.main_frame, text="Agent Reasoning & Logs", padding="10")
        self.log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.log_text = scrolledtext.ScrolledText(self.log_frame, state='disabled', height=15)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Redirect print() to this window
        sys.stdout = TextRedirector(self.log_text)
        sys.stderr = TextRedirector(self.log_text)

        # 3. Chat Interface
        self.chat_frame = ttk.LabelFrame(self.main_frame, text="Chat with Grok", padding="10")
        self.chat_frame.pack(fill=tk.X, pady=5)

        self.chat_input = ttk.Entry(self.chat_frame)
        self.chat_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.chat_input.bind("<Return>", lambda e: self.send_chat())

        self.btn_send = ttk.Button(self.chat_frame, text="Ask", command=self.send_chat)
        self.btn_send.pack(side=tk.RIGHT)

        self.chat_history = [{"role": "system", "content": "You are a helpful trading assistant. The user is running a stock bot."}]

    def log(self, message):
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}")

    def run_strategy_thread(self):
        self.btn_run.config(state='disabled')
        self.status_lbl.config(text="Status: Running Strategy...")
        t = threading.Thread(target=self.run_strategy)
        t.start()

    def run_strategy(self):
        try:
            # We create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            loop.run_until_complete(self.async_strategy())
            loop.close()
        except Exception as e:
            print(f"Error in strategy thread: {e}")
        finally:
            self.root.after(0, self.reset_ui)

    async def async_strategy(self):
        try:
            print("\n--- STARTING STRATEGY ---\n")
            if not ib.isConnected():
                print(f"Connecting to IBKR on port {IB_PORT}...")
                await ib.connectAsync('127.0.0.1', IB_PORT, clientId=16, timeout=20)
            
            watchlist = ["NVDA", "GOOGL", "AAPL"]
            strategy = await get_grok_strategy(watchlist)
            
            if strategy:
                # strategy is now just the allocation dict (reasoning was printed in get_grok_strategy)
                await trade_rebalance(strategy)
            else:
                print("No strategy generated.")

        except Exception as e:
            print(f"Strategy Error: {e}")

    def reset_ui(self):
        self.btn_run.config(state='normal')
        self.status_lbl.config(text="Status: Ready")

    def send_chat(self):
        user_text = self.chat_input.get()
        if not user_text.strip(): return
        
        self.chat_input.delete(0, tk.END)
        self.log(f"You: {user_text}")
        
        # Add to history
        self.chat_history.append({"role": "user", "content": sanitize_text(user_text)})
        
        # Run API call in thread to not freeze UI
        threading.Thread(target=self.get_chat_response).start()

    def get_chat_response(self):
        try:
            response = client.chat.completions.create(
                model="grok-3",
                messages=self.chat_history
            )
            reply = response.choices[0].message.content
            self.log(f"Grok: {reply}")
            self.chat_history.append({"role": "assistant", "content": reply})
        except Exception as e:
            print(f"Chat Error: {e}")

if __name__ == "__main__":
    root = tk.Tk()
    app = TradingDashboard(root)
    root.mainloop()
