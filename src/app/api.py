from fastapi import FastAPI
from pydantic import BaseModel
import asyncio
from .agents import run_client_profile
from .schemas import ClientProfileOutput

app = FastAPI(title="Samaia ADK API")

class ProfileRequest(BaseModel):
    company: str
    website: str
    doc_urls: list[str] | None = None

@app.post("/profile", response_model=ClientProfileOutput)
async def profile(req: ProfileRequest):
    return await run_client_profile(req.model_dump())