# src/app/schemas.py
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field


# ----------- OUTPUT SCHEMA (what the LLM must return) -----------

class ThreeLeggedStoolScores(BaseModel):
    resonance: int
    substantiation: int
    differentiation: int
    resonance_notes: Optional[str] = None
    substantiation_notes: Optional[str] = None
    differentiation_notes: Optional[str] = None


class ValueProposition(BaseModel):
    headlines: List[str] = Field(default_factory=list)
    support_points: List[str] = Field(default_factory=list)
    three_legged_stool: ThreeLeggedStoolScores


class Signals(BaseModel):
    evidence: List[str] = Field(default_factory=list)
    business_pillars: List[str] = Field(default_factory=list)


class AssetsOutput(BaseModel):
    public_assets: List[str] = Field(default_factory=list)
    internal_assets: List[str] = Field(default_factory=list)


class Client(BaseModel):
    name: Optional[str] = None
    size: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    homepage: Optional[str] = None
    industry: Optional[str] = None


class ClientProfileOutput(BaseModel):
    assets: AssetsOutput
    client: Client
    signals: Signals
    value_proposition: ValueProposition


# ----------- PROMPT SCHEMA (what we SEND the LLM as instructions/input) -----------

class AssetItem(BaseModel):
    fileName: str
    url: str
    filePath: str


class AssetsInput(BaseModel):
    publicAssets: List[AssetItem] = Field(default_factory=list)
    internalAssets: List[AssetItem] = Field(default_factory=list)


class CompanyInfo(BaseModel):
    name: str
    url: str


class InputBlock(BaseModel):
    companyInfo: CompanyInfo
    assets: Optional[AssetsInput] = None
    metadata: Optional[dict] = None


class QuestionsBlock(BaseModel):
    # We include all keys; default to empty containers so the LLM fills them.
    swot: dict = Field(default_factory=dict)
    offerings: List[dict] = Field(default_factory=list)
    icps_and_pains: dict = Field(default_factory=dict)
    risks_and_gaps: List[dict] = Field(default_factory=list)
    client_identity: dict = Field(default_factory=dict)
    evidence_and_trust: dict = Field(default_factory=dict)
    three_legged_stool: dict = Field(default_factory=dict)
    brand_and_messaging: dict = Field(default_factory=dict)
    competitive_context: dict = Field(default_factory=dict)
    attachment_citations: List[str] = Field(default_factory=list)
    growth_opportunities: List[dict] = Field(default_factory=list)
    positioning_and_value_prop: dict = Field(default_factory=dict)


class ResponseStyle(BaseModel):
    format: str = "json"
    language: str = "en"
    max_tokens: int = 12000
    avoid_fluff: bool = True
    compact_lists: bool = True


class AttachmentHandling(BaseModel):
    extraction_hints: List[str] = Field(
        default_factory=lambda: [
            "Look for case studies, testimonials, certification badges, compliance statements.",
            "Pull product/offer lists, feature tables, outcomes/metrics, and price/packaging hints.",
            "For slide decks: titles, agenda, 'why us', 'proof', and 'customers' slides are high-signal.",
            "For PDFs: executive summaries, methodology, results, and appendix metrics.",
        ]
    )
    max_tokens_per_asset: int = 8000
    prioritize_internal_assets: bool = True


class ClientPrompt(BaseModel):
    type: str = "object"
    title: str = "Client Prompt Schema (Gemini)"
    system_instructions: str = (
        "You are analyzing the CLIENT (the user's own organization). Use ONLY the provided input "
        "and attachments as your primary ground truth. When you assert a fact, map it to a specific "
        "attachment (fileName + page/slide/section if possible). If an item is unknown, say 'unknown'. "
        "Keep claims specific, measurable, and attributable. Avoid generic fluff."
    )
    task_objectives: List[str] = Field(
        default_factory=lambda: [
            "Identify the client's core value proposition and positioning from provided context.",
            "Extract ICPs, key pains, outcomes, and buying triggers.",
            "Build a 3-Legged Stool assessment (Resonance, Differentiation, Substantiation) with evidence.",
            "Inventory offerings, proof points, certifications, security/compliance signals.",
            "Summarize brand voice & messaging pillars; propose baseline messaging.",
            "Map evidence to attachments and highlight gaps/risks.",
        ]
    )
    input: InputBlock
    attachment_handling: AttachmentHandling = AttachmentHandling()
    questions: QuestionsBlock = QuestionsBlock()
    response_style: ResponseStyle = ResponseStyle()
    output_contract_ref: str = "client_profile_schema.json"


def build_prompt_payload(
    *,
    company_name: str,
    company_url: str,
    public_assets: Optional[list[dict]] = None,
    internal_assets: Optional[list[dict]] = None,
    metadata: Optional[dict] = None,
) -> ClientPrompt:
    """Produce a prompt object that conforms to your Prompt Schema."""
    assets_block = None
    if public_assets or internal_assets:
        assets_block = AssetsInput(
            publicAssets=[AssetItem(**a) for a in (public_assets or [])],
            internalAssets=[AssetItem(**a) for a in (internal_assets or [])],
        )

    return ClientPrompt(
        input=InputBlock(
            companyInfo=CompanyInfo(name=company_name, url=company_url),
            assets=assets_block,
            metadata=metadata,
        )
    )