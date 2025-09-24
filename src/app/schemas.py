from pydantic import BaseModel, Field
from typing import List, Optional

class ClientProfileOutput(BaseModel):
    company: str = Field(..., description="Company name.")
    website: str = Field(..., description="Company website.")
    summary: str = Field(..., description="One-paragraph summary.")
    key_points: List[str] = Field(default_factory=list, description="Bulleted highlights")
    confidence: float = Field(..., ge=0, le=1, description="Model confidence 0..1")

class ClientProfileInput(BaseModel):
    company: str
    website: str
    doc_urls: Optional[List[str]] = None  # you can ignore for now; we’ll wire RAG later