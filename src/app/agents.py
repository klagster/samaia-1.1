from typing import Any, Dict
from google.adk.agents import LlmAgent
from google.genai import types as gt
from .schemas import ClientProfileOutput

# System instructions keep it focused and JSON-only
SYSTEM_PROMPT = (
    "You are an analyst. Given a company name and website, return ONLY a JSON object "
    "that matches the provided Pydantic schema. Be concise, factual, and avoid fluff."
)

# Exported agent used by Runner in run.py
client_profile_agent = LlmAgent(
    name="client_profile_agent",
    model="gemini-2.0-flash",        # uses Vertex via ADC
    description="Builds a short company profile with typed JSON output.",
    instruction=SYSTEM_PROMPT,
    output_schema=ClientProfileOutput,   # ADK will validate/shape output
)

# Helper to build a user message for the Runner; no direct agent.run calls here
# (Runner is responsible for execution and streaming.)

def build_client_profile_message(payload: Dict[str, Any]) -> gt.Content:
    """Create a single user message from a simple payload.

    Expected payload keys: {"company": str, "website": str}
    """
    company = str(payload.get("company", "")).strip()
    website = str(payload.get("website", "")).strip()

    prompt = (
        f"Company: {company}\n"
        f"Website: {website}\n"
        "Return ONLY valid JSON for the schema `ClientProfileOutput`."
    )

    return gt.Content(role="user", parts=[gt.Part(text=prompt)])