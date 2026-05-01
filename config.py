import os
import json
import dataclasses
from dotenv import load_dotenv
from ib_async import IB
from openai import OpenAI
import anthropic

load_dotenv()

XAI_KEY       = os.getenv("XAI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
IB_HOST = os.getenv("IBKR_HOST", "127.0.0.1")

def _resolve_ib_settings():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(cfg_path) as f:
            mode = json.load(f).get("mode", "live")
    except Exception:
        mode = "live"
    if mode == "paper":
        port = int(os.getenv("IBKR_PORT_PAPER", 7497))
        acc  = os.getenv("IBKR_ACCOUNT_PAPER", "")
    else:
        port = int(os.getenv("IBKR_PORT_LIVE", 7496))
        acc  = os.getenv("IBKR_ACCOUNT_LIVE", "")
    return port, acc, mode

IB_PORT, IB_ACC, TRADING_MODE = _resolve_ib_settings()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

BOT_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclasses.dataclass
class AppContext:
    ib:             IB
    client:         OpenAI       # xAI Grok — used for Pass A (live search)
    claude:         anthropic.Anthropic  # Anthropic Claude — used for Pass B (reasoning)
    ib_acc:         str


def create_context() -> AppContext:
    return AppContext(
        ib=IB(),
        client=OpenAI(api_key=XAI_KEY, base_url="https://api.x.ai/v1"),
        claude=anthropic.Anthropic(api_key=ANTHROPIC_KEY),
        ib_acc=IB_ACC,
    )


def load_config() -> dict:
    path = os.path.join(BOT_DIR, "config.json")
    with open(path) as f:
        return json.load(f)
