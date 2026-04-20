import os
import json
import dataclasses
from dotenv import load_dotenv
from ib_async import IB
from openai import OpenAI

load_dotenv()

XAI_KEY = os.getenv("XAI_API_KEY")
IB_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IBKR_PORT", 7497))
IB_ACC  = os.getenv("IBKR_ACCOUNT")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

BOT_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclasses.dataclass
class AppContext:
    ib: IB
    client: OpenAI
    ib_acc: str


def create_context() -> AppContext:
    return AppContext(
        ib=IB(),
        client=OpenAI(api_key=XAI_KEY, base_url="https://api.x.ai/v1"),
        ib_acc=IB_ACC,
    )


def load_config() -> dict:
    path = os.path.join(BOT_DIR, "config.json")
    with open(path) as f:
        return json.load(f)
