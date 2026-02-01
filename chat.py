import os
from openai import OpenAI
from dotenv import load_dotenv
from xai_sdk import Client
from xai_sdk.chat import user, system, assistant
from xai_sdk.tools import web_search
load_dotenv()
client = Client(
    api_key=os.getenv("XAI_API_KEY"),
    timeout=3600, # Override default timeout with longer timeout for reasoning models
)
with_web_search = input("Do you want to use web search for this session? (y/n): ")
if with_web_search.lower() == "y":
    tools = [web_search()]
else:
    tools = []
chat = client.chat.create(model="grok-4", tools=tools)
while True:
    console = input("Enter your prompt: ")
    chat.append(user("reply briefly and concisely (maximum 100 words)|Message: " + console))
    response = chat.sample()
    print(response.content)