import asyncio
import os
import json
import re
from dotenv import load_dotenv
from ib_async import * # Modern library for Python 3.14
from openai import OpenAI
import time
import logging
import datetime

# Configure Logging
logging.basicConfig(
    filename='bot_activity.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 1. SETUP
load_dotenv()
XAI_KEY = os.getenv("XAI_API_KEY")
IB_PORT = int(os.getenv("IBKR_PORT", 7497))
IB_ACC = os.getenv("IBKR_ACCOUNT")

client = OpenAI(api_key=XAI_KEY, base_url="https://api.x.ai/v1")
ib = IB()

def log_event(message, level="info"):
    print(message)
    if level == "info": logging.info(message)
    elif level == "error": logging.error(message)
    elif level == "warning": logging.warning(message)


# 2. DATA SANITIZER (Protects your account details from AI training)
def sanitize_text(text):
    text = re.sub(r'[Uu]\d{4,10}', '[ACCOUNT_ID]', text)
    text = re.sub(r'\$?\d{1,3}(,\d{3})*(\.\d+)?', '[AMOUNT]', text)
    return text

# 3. GET TOTAL CAPITAL (NLV)
async def get_total_capital():
    # ib.accountValues() is an async call in this library
    for v in ib.accountValues():
        if v.tag == 'NetLiquidation' and v.currency == 'USD' and v.account == IB_ACC:
            return float(v.value)
    return 0.0

# 4. GROK STRATEGY ANALYST
async def get_grok_strategy(watchlist, history=None):
    log_event(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] SEARCHING WEB & X: Analyzing sentiment for {', '.join(watchlist)}...")
    
    # Read dynamic instructions from file
    user_instructions = ""
    if os.path.exists("bot_instructions.txt"):
        with open("bot_instructions.txt", "r") as f:
            user_instructions = f.read().strip()
            if user_instructions:
                log_event(f"Applying User Instructions: {user_instructions}")

    prompt = f"""
    Analyze live news for {', '.join(watchlist)} using X (Twitter) and web search.
    Today is {datetime.datetime.now().strftime('%b %d, %Y')}.
    
    USER OVERRIDE INSTRUCTIONS:
    {user_instructions}
    
    You MUST return a JSON object with two fields:
    1. "reasoning": A concise list of bullet points explaining your decision. Cite sources like "(via X)" or "(via Web)".
    2. "allocation": A dictionary with the percentages for each stock (must sum to roughly 100%).
    
    Example JSON structure:
    {{
        "reasoning": "- GOOGL up on earnings leak (via X)\\n- AAPL neutral, waiting for event (via Web)",
        "allocation": {{'NVDA': 10, 'GOOGL': 50, 'AAPL': 40}}
    }}
    """
    
    try:
        messages = []
        if history is not None:
            messages = history
            messages.append({"role": "user", "content": sanitize_text(prompt)})
        else:
            messages = [{"role": "user", "content": sanitize_text(prompt)}]

        response = client.chat.completions.create(
            # model="grok-3", # Switching to a known stable model identifier
            messages=messages,
            # extra_body={"search_parameters": {"mode": "on"}} # Enable if needed for specific models
        )
        
        # Grok will now execute the search server-side and return the final answer
        content = response.choices[0].message.content
        
        if history is not None:
            history.append({"role": "assistant", "content": content})

        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            data = json.loads(match.group())
            # Handle both old format (direct dict) and new format (nested dict)
            if "allocation" in data:
                reasoning = data.get('reasoning', 'No reasoning provided.')
                log_event(f"GROK REASONING: {reasoning}")
                return data["allocation"]
            else:
                return data
        return None
    except Exception as e:
        log_event(f"Agent API Error: {e}", "error")
        # Debugging: Print full response if possible or raw error
        if 'response' in locals():
            print(f"Raw Response: {response}")
        return None
# 5. EXECUTE REBALANCE (BRACKET ORDERS)
import time
import asyncio
from ib_async import *

async def trade_rebalance(strategy):
    # Refresh positions
    positions = await ib.reqPositionsAsync()
    current_positions = {p.contract.symbol: p.position for p in positions}
    
    total_val = await get_total_capital() * 0.98 
    log_event(f"Rebalancing with LIVE DATA and URGENT ALGO (Equity: ${total_val:.2f})...")

    for ticker, percentage in strategy.items():
        if ticker not in ["NVDA", "GOOGL", "AAPL"]: continue
        
        target_amt = (percentage / 100) * total_val
        contract = Stock(ticker, 'SMART', 'USD')
        await ib.qualifyContractsAsync(contract)
        
        # 1. FORCE LIVE DATA MODE
        ib.reqMarketDataType(1) 
        [ticker_data] = await ib.reqTickersAsync(contract)
        price = ticker_data.marketPrice() or ticker_data.last
        
        target_qty = int(target_amt / price)
        current_qty = current_positions.get(ticker, 0)
        delta_qty = target_qty - current_qty
        
        if delta_qty > 0:
            log_event(f"URGENT BUY: {ticker} (+{delta_qty} shares)")
            
            # 2. THE SPEED UP: Use Adaptive Algo
            buy_order = MarketOrder('BUY', delta_qty, account=IB_ACC)
            buy_order.algoStrategy = 'Adaptive'
            buy_order.algoParams = [TagValue('priority', 'Urgent')] # Aggressive fill
            buy_order.tif = 'GTC'
            buy_order.transmit = True 
            
            trade = ib.placeOrder(contract, buy_order)
            
            # Wait for fill (Should be much faster now)
            start_wait = time.time()
            while not trade.isDone() and (time.time() - start_wait < 20):
                await asyncio.sleep(0.2) # Faster polling
            
            if trade.isDone() and trade.orderStatus.status == 'Filled':
                log_event(f"SUCCESS: {ticker} filled instantly. Attaching Stop Loss.")
                # Attach Protection
                sl = StopOrder('SELL', delta_qty, round(price * 0.93, 2), account=IB_ACC, tif='GTC')
                tp = LimitOrder('SELL', delta_qty, round(price * 1.15, 2), account=IB_ACC, tif='GTC')
                ib.placeOrder(contract, sl)
                ib.placeOrder(contract, tp)

        elif delta_qty < -1:
            log_event(f"URGENT SELL: {ticker} (-{abs(delta_qty)})")
            sell_order = MarketOrder('SELL', abs(delta_qty), account=IB_ACC, tif='GTC')
            sell_order.algoStrategy = 'Adaptive'
            sell_order.algoParams = [TagValue('priority', 'Urgent')]
            ib.placeOrder(contract, sell_order)
    # 1. Refresh positions
    positions = await ib.reqPositionsAsync()
    current_positions = {p.contract.symbol: p.position for p in positions}
    
    total_val = await get_total_capital() * 0.98 
    log_event(f"Rebalancing Cash Account (Equity: ${total_val:.2f})...")

    for ticker, percentage in strategy.items():
        if ticker not in ["NVDA", "GOOGL", "AAPL"]: continue
        
        target_amt = (percentage / 100) * total_val
        contract = Stock(ticker, 'SMART', 'USD')
        await ib.qualifyContractsAsync(contract)
        
        # Get live price
        ib.reqMarketDataType(1) 
        [ticker_data] = await ib.reqTickersAsync(contract)
        price = ticker_data.marketPrice() or ticker_data.last
        
        target_qty = int(target_amt / price)
        current_qty = current_positions.get(ticker, 0)
        delta_qty = target_qty - current_qty
        
        # --- CASH ACCOUNT BUY LOGIC ---
        if delta_qty > 0:
            log_event(f"CASH BUY: Sending {ticker} (+{delta_qty} shares)")
            
            # Place only the BUY order first
            buy_order = MarketOrder('BUY', delta_qty, account=IB_ACC)
            buy_order.tif = 'GTC' # Fixes the 10349 Preset Error
            buy_order.transmit = True 
            
            trade = ib.placeOrder(contract, buy_order)
            
            # WAIT FOR FILL (Max 30 seconds)
            log_event(f"Waiting for {ticker} fill to avoid short-sale rejection...")
            start_wait = time.time()
            while not trade.isDone() and time.time() - start_wait < 30:
                await asyncio.sleep(1)
            
            if trade.isDone() and trade.orderStatus.status == 'Filled':
                log_event(f"FILLED. Attaching Protection (Stop: 7%, Profit: 15%).")
                
                # Protect the shares we now actually own
                sl = StopOrder('SELL', delta_qty, round(price * 0.93, 2), account=IB_ACC, tif='GTC')
                tp = LimitOrder('SELL', delta_qty, round(price * 1.15, 2), account=IB_ACC, tif='GTC')
                
                # These are sent separately but since we have the shares, they won't reject.
                ib.placeOrder(contract, sl)
                ib.placeOrder(contract, tp)
            else:
                log_event(f"BUY order for {ticker} did not fill. Skipping protection.", "warning")

        # --- REDUCE POSITION (SELL) ---
        elif delta_qty < -1:
            log_event(f"CASH SELL: Reducing {ticker} by {abs(delta_qty)} shares")
            sell_order = MarketOrder('SELL', abs(delta_qty), account=IB_ACC, tif='GTC')
            ib.placeOrder(contract, sell_order)
    # 1. CANCEL ALL EXISTING OPEN ORDERS FOR OUR WATCHLIST
    # This prevents 'Double Buying' or orders getting stuck in TWS
    log_event("Cleaning up existing open orders...")
    trades = await ib.reqAllOpenOrdersAsync()
    for t in trades:
        if t.contract.symbol in ["NVDA", "GOOGL", "AAPL"]:
            ib.cancelOrder(t.order)
    
    # 2. GET CURRENT POSITIONS
    positions = await ib.reqPositionsAsync()
    current_positions = {p.contract.symbol: p.position for p in positions}
    
    total_val = await get_total_capital() * 0.98 # 2% cash buffer
    log_event(f"Rebalancing Portfolio (Total Equity: ${total_val:.2f})...")

    for ticker, percentage in strategy.items():
        if ticker not in ["NVDA", "GOOGL", "AAPL"]: continue
        
        target_amt = (percentage / 100) * total_val
        contract = Stock(ticker, 'SMART', 'USD')
        await ib.qualifyContractsAsync(contract)
        
        # 3. GET LIVE MARKET PRICE
        ib.reqMarketDataType(1) # Live
        [ticker_data] = await ib.reqTickersAsync(contract)
        price = ticker_data.marketPrice() or ticker_data.last or ticker_data.close
        
        if price != price or price <= 0:
             log_event(f"SKIPPING {ticker}: Invalid price data.", "warning")
             continue

        # 4. CALCULATE DELTA (What we have vs What we want)
        target_qty = int(target_amt / price)
        current_qty = current_positions.get(ticker, 0)
        delta_qty = target_qty - current_qty
        
        # 5. EXECUTE WITH FULL TRANSMISSION
        if abs(delta_qty) >= 1:
            if delta_qty > 0:
                # BUY: Bracket Order (Parent + Stop Loss)
                log_event(f"BUYING {delta_qty} of {ticker}...")
                parent = MarketOrder('BUY', delta_qty, account=IB_ACC)
                parent.transmit = False # Hold until SL is attached
                
                stop_loss = StopOrder('SELL', delta_qty, round(price * 0.93, 2), account=IB_ACC)
                stop_loss.parentId = parent.orderId
                stop_loss.transmit = True # THIS TRIGER THE ACTUAL TRADE
                
                ib.placeOrder(contract, parent)
                ib.placeOrder(contract, stop_loss)
            
            else:
                # SELL: Direct Market Sell to reduce position
                log_event(f"SELLING {abs(delta_qty)} of {ticker}...")
                sell_order = MarketOrder('SELL', abs(delta_qty), account=IB_ACC)
                sell_order.transmit = True
                ib.placeOrder(contract, sell_order)
        else:
             log_event(f"STABLE: {ticker} (No action needed)")
# 6. MAIN ENGINE
async def main():
    try:
        log_event(f"Connecting to IBKR on port {IB_PORT}...")
        await ib.connectAsync('127.0.0.1', IB_PORT, clientId=15, timeout=60)
        
        watchlist = ["NVDA", "GOOGL", "AAPL"]
        
        while True:
            log_event("\n--- STARTING 5-MINUTE CYCLE ---")
            
            # Check market news/status via Grok
            strategy = await get_grok_strategy(watchlist)
            
            if strategy:
                await trade_rebalance(strategy)
            
            log_event("Cycle complete. Sleeping for 1 minute...")
            await asyncio.sleep(300) # 300 seconds = 5 minutes
            
    except Exception as e:
        log_event(f"Bot Error: {e}", "error")
    finally:
        ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())