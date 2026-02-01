import asyncio
import os
from dotenv import load_dotenv
from ib_async import * # Using the modern library

# 1. Load your .env
load_dotenv()

async def check_connection():
    ib = IB()
    port = int(os.getenv("IBKR_PORT", 7497))
    target_account = os.getenv("IBKR_ACCOUNT")

    print(f"Connecting to 127.0.0.1:{port}...")
    
    try:
        # connectAsync is better for Python 3.14 on Mac
        await ib.connectAsync('127.0.0.1', port, clientId=10, timeout=5)
        
        print(f"Connected to Account: {ib.wrapper.accounts[0]}")
        print(f"Targeting Account from .env: {target_account}")

        if ib.wrapper.accounts[0] == target_account:
            print("✅ SUCCESS: Your script is correctly linked to your account.")
        else:
            print("⚠️ WARNING: Account ID mismatch. Check your .env file.")

        # Stay connected for 2 seconds to prove it's stable
        await asyncio.sleep(2)
        ib.disconnect()

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("Tip: Check TWS > API Settings > Trusted IPs includes 127.0.0.1")

if __name__ == "__main__":
    # The correct way to run a script in Python 3.14
    asyncio.run(check_connection())