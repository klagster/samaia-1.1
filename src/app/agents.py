# src/app/agents.py
from __future__ import annotations
from google.adk.agents import LlmAgent
from google.adk.models.google_llm import GoogleLLM
from google.adk.types import Content, TextPart, Role

from .schemas import ClientProfileOutput, build_prompt_payload


# Single-model config (Vertex/Gemini; environment is handled in run.py)
MODEL = GoogleLLM(
    model="gemini-1.5-pro",  # change if you want
    temperature=0.2,
)

# System instruction: we’ll let the Prompt Schema carry most of the structure,
# but reiterate “return ONLY JSON confirming to output schema”
SYSTEM_TXT = (
    "You are a Client Profiling Agent. You will receive a JSON object that strictly "
    "follows the 'Client Prompt Schema (Gemini)'. Use ONLY the provided inputs and "
    "attachments as ground truth. Your task is to produce a JSON object that conforms "
    "to the required output schema (ClientProfileOutput). Return ONLY valid JSON—no prose."
)

client_profile_agent = LlmAgent(
    name="client_profile_agent",
    description="Builds a structured client profile using the given prompt schema and attachments.",
    model=MODEL,
    instruction=SYSTEM_TXT,
    # CRITICAL: enforce the final structure
    output_schema=ClientProfileOutput,
    # Remove transfer to avoid the warning
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)


def build_client_profile_message(payload: dict) -> Content:
    """
    Turn an easy payload into the full Prompt Schema JSON the model consumes.
    payload expects keys: company, website, doc_urls? (optional)
    Optionally: assets as arrays of {fileName,url,filePath}
    """
    company = payload.get("company") or "Unknown"
    website = payload.get("website") or "https://example.com"

    # If user passes doc_urls (simple strings), map them into publicAssets items
    doc_urls = payload.get("doc_urls") or []
    public_assets = [
        {"fileName": url.split("/")[-1] or "doc", "url": url, "filePath": url}
        for url in doc_urls
    ]

    # Pass through richer assets if provided
    public_assets = payload.get("public_assets") or public_assets
    internal_assets = payload.get("internal_assets") or []

    prompt = build_prompt_payload(
        company_name=company,
        company_url=website,
        public_assets=public_assets,
        internal_assets=internal_assets,
        metadata=payload.get("metadata"),
    )

    # ADK Content object with the entire prompt schema as the user message
    return Content(
        role=Role.USER,
        parts=[TextPart(text=prompt.model_dump_json())],
    )