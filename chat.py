import os
from openai import OpenAI
from dotenv import load_dotenv
from xai_sdk import Client
from xai_sdk.chat import user, system, assistant
from xai_sdk.tools import web_search
load_dotenv()

#########################################################
def main(assistantName="Grok", model="grok-4", with_web_search=False):
    client = Client(
        api_key=os.getenv("XAI_API_KEY"),
        timeout=3600, # Override default timeout with longer timeout for reasoning models
    )
    if with_web_search:
        tools = [web_search()]
    else:
        tools = []
    chat = client.chat.create(model=model, tools=tools)
    while True:
        print("--------------------------------")
        console = input("Enter your prompt: ")
        chat.append(user("Message: " + console))
        response = chat.sample()
        print(f"{assistantName}: {response.content}")
#########################################################
assistantName = input("Enter your assistant name or leave blank for default (Grok): ")
if assistantName == "":
    assistantName = "Grok"
model = input("Enter the model name or leave blank for default (grok-4): ")
if model == "":
    model = "grok-4"
websearch = False
askWebSearch = input("Do you want to use web search for this session? (y/n): ")
if askWebSearch.lower() == "y":
    websearch = True
else:
    websearch = False

print(f"Loading Model: {model}")
print(f"Web Search is {'enabled' if websearch else 'disabled'}")
main(assistantName, model, websearch)