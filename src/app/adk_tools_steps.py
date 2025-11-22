from typing import Dict, Any, List, Optional

from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app import (
    step1_evidence_grabber,
    step2_evidence_harvester,
    step3_hypotheses_generator,
    step4_alignment,
    step5_compelling_events,
)


async def step1_tool_fn(
    ctx: ToolContext,
    company: str,
    company_url: Optional[str] = None,
    domain: Optional[str] = None,
    max_results: int = 500,
) -> Dict[str, Any]:
    """ADK tool wrapper for Step 1: collect web evidence."""
    events: List[Dict[str, Any]] = step1_evidence_grabber.run_step(
        company=company,
        company_url=company_url,
        domain=domain,
        max_results=max_results,
    )

    # Store into ADK session state (same keys we already set from run.py)
    ctx.session.state["step1:web_evidence"] = events
    await ctx.session_service.update_session(ctx.session)

    return {
        "web_evidence": events,
        "count": len(events),
    }


step1_tool = FunctionTool(
    func=step1_tool_fn,
    name="step1_collect_web_evidence",
    description="Collect grounded web evidence for the target company.",
)


async def step2_tool_fn(
    ctx: ToolContext,
    company: Optional[str] = None,
) -> Dict[str, Any]:
    """ADK tool wrapper for Step 2: harvest and normalize evidence into problems.

    Reads Step 1 web evidence from session state and produces a structured
    problems index for downstream steps.
    """
    # Pull company name and web evidence from the session state.
    # Fall back to the explicit company parameter if provided.
    session_company = ctx.session.state.get("company_name")
    company = company or session_company or "Unknown company"

    web_evidence: List[Dict[str, Any]] = ctx.session.state.get("step1:web_evidence", [])
    if not isinstance(web_evidence, list):
        web_evidence = []

    problems: Dict[str, Any] = step2_evidence_harvester.run_step(
        company=company,
        raw_events=web_evidence,
    ) or {}

    # Store into ADK session state (same keys we already set from run.py)
    ctx.session.state["step2:problems"] = problems
    await ctx.session_service.update_session(ctx.session)

    # Provide a simple, structured response for the agent / caller.
    count = 0
    if isinstance(problems, dict):
        items = problems.get("evidence") or problems.get("problems")
        if isinstance(items, list):
            count = len(items)

    return {
        "company": company,
        "problems": problems,
        "count": count,
    }


step2_tool = FunctionTool(
    func=step2_tool_fn,
    name="step2_harvest_problems",
    description="Normalize and score collected web evidence into structured problems for the target company.",
)


async def step3_tool_fn(
    ctx: ToolContext,
    company: Optional[str] = None,
    time_window: Optional[str] = None,
    max_per_bucket: int = 3,
) -> Dict[str, Any]:
    """ADK tool wrapper for Step 3: generate hypotheses from evidence.

    Uses the LLM-backed Step 3 implementation to synthesize evidenced
    problems/hypotheses from the normalized evidence produced by Step 2.

    Reads:
      - company_name, time_window from session.state
      - step2:problems as the evidence index

    Writes:
      - step3:hypotheses into session.state
    """
    # Resolve company and time window from session state if not explicitly provided.
    session_company = ctx.session.state.get("company_name")
    company = company or session_company or "Unknown company"

    session_tw = ctx.session.state.get("time_window") or "last 12–18 months"
    time_window = time_window or session_tw

    # Evidence index comes from Step 2 output stored in session.state.
    evidence_index: Dict[str, Any] = ctx.session.state.get("step2:problems") or {}
    if not isinstance(evidence_index, dict):
        evidence_index = {}

    hypotheses: Dict[str, Any] = step3_hypotheses_generator.run_step(
        evidence_index=evidence_index,
        company=company,
        time_window=time_window,
        max_per_bucket=max_per_bucket,
    ) or {}

    # Store into ADK session state (same keys we already set from run.py)
    ctx.session.state["step3:hypotheses"] = hypotheses
    await ctx.session_service.update_session(ctx.session)

    problems = []
    if isinstance(hypotheses, dict):
        problems = hypotheses.get("problems") or []
        if not isinstance(problems, list):
            problems = []

    return {
        "company": company,
        "time_window": time_window,
        "hypotheses": hypotheses,
        "problem_count": len(problems),
    }


step3_tool = FunctionTool(
    func=step3_tool_fn,
    name="step3_generate_hypotheses",
    description="Generate evidenced business problems/hypotheses from normalized evidence using the LLM-backed Step 3.",
)


async def step4_tool_fn(
    ctx: ToolContext,
    top_k: int = 3,
    company: Optional[str] = None,
    time_window: Optional[str] = None,
    taxonomy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """ADK tool wrapper for Step 4: align hypotheses to the customer challenge taxonomy.

    Uses the LLM-backed Step 4 implementation to map evidenced problems
    (Step 3 output) to the most relevant taxonomy challenges.

    Reads:
      - company_name, time_window from session.state
      - step3:hypotheses as the issues input
      - taxonomy either from the explicit argument or from session.state

    Writes:
      - step4:alignments into session.state
    """
    # Resolve company and time window from session state if not explicitly provided.
    session_company = ctx.session.state.get("company_name")
    company = company or session_company or "Unknown company"

    session_tw = ctx.session.state.get("time_window") or "last 12–18 months"
    time_window = time_window or session_tw

    # Issues (problems/hypotheses) come from Step 3 output in session.state.
    issues: Dict[str, Any] = ctx.session.state.get("step3:hypotheses") or {}
    if not isinstance(issues, dict):
        issues = {}

    # Taxonomy can be passed in explicitly or pulled from the session.
    if taxonomy is None:
        taxonomy = (
            ctx.session.state.get("step4:taxonomy")
            or ctx.session.state.get("customer_challenges_taxonomy")
            or {}
        )
    if not isinstance(taxonomy, dict) or not taxonomy:
        raise ValueError(
            "Step 4 tool requires a taxonomy dict; none found in arguments or session.state."
        )

    aligned: Dict[str, Any] = step4_alignment.run_step(
        issues=issues,
        taxonomy=taxonomy,
        top_k=top_k,
        company=company,
        time_window=time_window,
    ) or {}

    # Store into ADK session state (same keys we already set from run.py)
    ctx.session.state["step4:alignments"] = aligned
    # Remember taxonomy location for downstream tools if needed.
    ctx.session.state.setdefault("step4:taxonomy", taxonomy)
    await ctx.session_service.update_session(ctx.session)

    alignments = []
    if isinstance(aligned, dict):
        alignments = aligned.get("alignments") or []
        if not isinstance(alignments, list):
            alignments = []

    return {
        "company": company,
        "time_window": time_window,
        "alignments": aligned,
        "alignment_count": len(alignments),
    }


step4_tool = FunctionTool(
    func=step4_tool_fn,
    name="step4_align_to_taxonomy",
    description="Align evidenced problems/hypotheses to the customer challenge taxonomy using the LLM-backed Step 4.",
)


async def step5_tool_fn(
    ctx: ToolContext,
    company: Optional[str] = None,
    strict: str = "medium",
    max_sources: int = 3,
) -> Dict[str, Any]:
    """ADK tool wrapper for Step 5: generate compelling events.

    Uses the LLM-backed Step 5 implementation to turn aligned issues
    and evidenced problems into executive-ready compelling events.

    Reads:
      - company_name from session.state
      - step2:problems as the problems input
      - step4:alignments as the alignments input

    Writes:
      - step5:compelling_events into session.state
    """
    # Resolve company from session state if not explicitly provided.
    session_company = ctx.session.state.get("company_name")
    company = company or session_company or "Unknown company"

    # Problems come from Step 2 output in session.state.
    problems_obj: Any = ctx.session.state.get("step2:problems") or {}
    if isinstance(problems_obj, dict):
        problems_list = problems_obj.get("problems") or problems_obj
    else:
        problems_list = problems_obj

    if not isinstance(problems_list, list):
        problems_list = []

    # Alignments come from Step 4 output in session.state.
    align_obj: Any = ctx.session.state.get("step4:alignments") or {}
    if isinstance(align_obj, dict):
        align_list = align_obj.get("alignments") or align_obj
    else:
        align_list = align_obj

    if not isinstance(align_list, list):
        align_list = []

    # Call the core Step 5 implementation.
    events_obj: Dict[str, Any] = step5_compelling_events.run_step(
        problems=problems_list,
        alignments=align_list,
        extra_evidence=None,
        strict=strict,
        max_sources=max_sources,
        company=company,
    ) or {}

    # Store into ADK session state for downstream access.
    ctx.session.state["step5:compelling_events"] = events_obj
    await ctx.session_service.update_session(ctx.session)

    compelling = []
    if isinstance(events_obj, dict):
        compelling = events_obj.get("compelling_events") or []
        if not isinstance(compelling, list):
            compelling = []

    return {
        "company": company,
        "events": events_obj,
        "event_count": len(compelling),
    }


step5_tool = FunctionTool(
    func=step5_tool_fn,
    name="step5_generate_compelling_events",
    description="Generate executive-ready compelling events from aligned issues using the LLM-backed Step 5.",
)