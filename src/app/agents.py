from google.adk.agents import Agent  # ADK import
from google.adk.tools import google_search  # built-in tool

# A tiny example agent; swap the model if you prefer
search_assistant = Agent(
    name="search_assistant",
    model="gemini-2.5-flash",
    instruction="You are a helpful assistant. Use Google Search when needed.",
    description="Assistant that can search the web.",
    tools=[google_search],
)