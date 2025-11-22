#!/usr/bin/env python3
"""
Step 5 – Compelling Events Generator

Takes:
  - problems: list of evidenced problems from Step 2
  - alignments: list of issue→campaign-challenge alignments from Step 4
  - extra_evidence: raw web events from Step 1 (optional)
…and produces:
  {
    "step": "compelling_events",
    "company_name": "<company>",
    "generated_at": "<iso>",
    "compelling_events": [
      {
        "issue_title": "...",
        "stakeholder": "...",
        "event_message": "...",
        "risk_if_ignored": "...",
        "urgency_trigger": "...",
        "opportunity_if_addressed": "...",
        "aligned_challenges": [...],
        "sources": [...],
        "evidence_stats": {
          "total_sources": int,
          "issue_specific_sources": int,
        },
        "confidence": float,
      },
      ...
    ]
  }

This module is meant to be the *only* place where compelling event
text is generated. run.py just passes inputs in and persists the result.

Updates in this patch:
- **LLM Retry Logic**: Implemented exponential backoff and retries for Gemini API calls
  to handle transient 429 RESOURCE_EXHAUSTED errors using tenacity.
"""

from __future__ import annotations

import os
import math
import logging
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import json

# --- NEW IMPORTS FOR RETRY LOGIC ---
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception
from google.genai.errors import ClientError
# -----------------------------------

from google import genai
from google.genai import types as genai_types
from google.genai.types import HttpOptions

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _safe_get(d: Dict[str, Any], key: str, default: Any = None) -> Any:
    return d.get(key, default) if isinstance(d, dict) else default


def _flatten_sources_from_problem(problem: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Step 2 problems usually look like:
      {
        "id": "...",
        "issue_id": "...",
        "label": "...",
        "evidence": [
          {
            "title": "...",
            "url": "...",
            "source": "vertex_search",
            "date_iso": "...",
            "snippet": "...",
            ...
          },
          ...
        ],
        ...
      }
    """
    ev = _safe_get(problem, "evidence", [])
    if not isinstance(ev, list):
        return []
    return [s for s in ev if isinstance(s, dict)]


def _collect_issue_evidence(
    problems: List[Dict[str, Any]],
    issue_id: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Collect all evidence rows from problems where issue_id matches.
    If issue_id is missing, falls back to all evidence.
    """
    collected: List[Dict[str, Any]] = []
    for p in problems:
        if not isinstance(p, dict):
            continue
        if issue_id and p.get("issue_id") != issue_id:
            continue
        collected.extend(_flatten_sources_from_problem(p))
    return collected


def _merge_sources(
    primary: List[Dict[str, Any]],
    secondary: List[Dict[str, Any]],
    max_total: int,
) -> List[Dict[str, Any]]:
    """
    Deduplicate sources by (url, title) and cap at max_total.
    """
    seen = set()
    merged: List[Dict[str, Any]] = []
    for src in list(primary) + list(secondary):
        if not isinstance(src, dict):
            continue
        url = src.get("url") or ""
        title = src.get("title") or ""
        key = (url.strip(), title.strip())
        if key in seen:
            continue
        seen.add(key)
        merged.append(src)
        if len(merged) >= max_total:
            break
    return merged


def _parse_date_iso(src: Dict[str, Any]) -> Optional[datetime]:
    """
    Try to interpret any of these as an ISO-like date:
      - source_date_iso
      - date_iso
      - published_at
      - collected_at
    """
    for k in ("source_date_iso", "date_iso", "published_at", "collected_at"):
        val = src.get(k)
        if not val or not isinstance(val, str):
            continue
        try:
            # handle 'Z' suffix
            clean = val.replace("Z", "+00:00") if val.endswith("Z") else val
            return datetime.fromisoformat(clean)
        except Exception:
            continue
    return None


def _score_evidence_recency(sources: List[Dict[str, Any]], window_months: int = 18) -> float:
    """
    Very rough recency score: 0..1
    - 1.0 ≈ most evidence within window_months
    - 0.0 ≈ no parseable dates
    """
    if not sources:
        return 0.0
    now = datetime.utcnow()
    # approximate months ~ 30 days
    window_days = window_months * 30
    in_window = 0
    dated = 0
    for s in sources:
        dt = _parse_date_iso(s)
        if not dt:
            continue
        dated += 1
        age_days = (now - dt).days
        if age_days <= window_days:
            in_window += 1
    if dated == 0:
        return 0.0
    frac = in_window / max(dated, 1)
    return max(0.0, min(1.0, frac))


def _score_evidence_density(total_sources: int) -> float:
    """
    Map "how much evidence" → 0..1 with diminishing returns.
    """
    if total_sources <= 0:
        return 0.0
    # log-ish curve: 1–2 sources ~0.4-0.6, 3–5 ~0.7-0.85, >8 ~0.95+
    return max(0.0, min(1.0, math.log10(total_sources + 1) / math.log10(9)))


def _score_confidence(
    issue_specific_sources: int,
    total_sources: int,
    recency_score: float,
    strict_level: str,
) -> float:
    """
    Heavily weight **issue-specific** evidence and recency, then density.
    """
    if total_sources <= 0:
        return 0.0

    spec_ratio = issue_specific_sources / total_sources if total_sources else 0.0
    density = _score_evidence_density(total_sources)

    strict_boost = {
        "low": 0.85,
        "medium": 1.0,
        "high": 1.1,
    }.get(strict_level, 1.0)

    base = (
        0.5 * spec_ratio +     # most weight: how focused the evidence is
        0.3 * recency_score +  # freshness matters
        0.2 * density          # more evidence is still good
    )
    return round(max(0.0, min(1.0, base * strict_boost)), 2)


# ---------------------------------------------------------------------
# JSON sanitization helper
# ---------------------------------------------------------------------

def _sanitize_json_like(text: str) -> str:
    """
    Best-effort cleanup of LLM JSON-like output:
    - Strips surrounding markdown fences (```).
    - Replaces literal newlines inside JSON strings with spaces.
    This helps when the model returns almost-JSON with line-wrapped strings.
    """
    if not text or not isinstance(text, str):
        return text

    t = text.strip()

    # Strip leading/trailing markdown code fences if present.
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].rstrip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)

    cleaned_chars: list[str] = []
    in_string = False
    escape = False

    for ch in t:
        if escape:
            cleaned_chars.append(ch)
            escape = False
            continue
        if ch == "\\":
            cleaned_chars.append(ch)
            escape = True
            continue
        if ch == "\"":
            cleaned_chars.append(ch)
            in_string = not in_string
            continue
        if in_string and ch in ("\n", "\r"):
            # Newlines inside JSON strings are illegal; normalize to space.
            cleaned_chars.append(" ")
            continue
        cleaned_chars.append(ch)

    return "".join(cleaned_chars)


def _extract_first_json_obj(text: str) -> Optional[Dict[str, Any]]:
    """
    Best-effort extraction of the first top-level JSON object from a string.
    This is used as a salvage path when the LLM returns almost-JSON with
    extra text around it.
    """
    if not text or not isinstance(text, str):
        return None

    # First, sanitize obvious JSON issues (e.g., newlines inside strings).
    text = _sanitize_json_like(text)

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start=start):
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            # Failed to parse, break inner loop and search for next {
                            break
        start = text.find("{", start + 1)
    return None

def _default_stakeholder_from_challenges(challenges: List[str]) -> str:
    """
    Very simple heuristic to pick a stakeholder if none is given.
    You can refine this mapping as you learn more.
    """
    joined = " ".join(challenges).lower()
    if any(k in joined for k in ("security", "breach", "hipaa", "gdpr", "iso 27001", "soc 2")):
        return "CISO / VP Security"
    if any(k in joined for k in ("compliance", "audit", "regulator")):
        return "Chief Compliance Officer"
    if any(k in joined for k in ("customer", "cx", "contact center", "service")):
        return "Chief Customer Officer / VP, Customer Experience"
    if any(k in joined for k in ("operations", "efficiency", "productivity")):
        return "COO / VP, Operations"
    if any(k in joined for k in ("cloud", "infrastructure", "it", "data center")):
        return "CIO / VP, IT Infrastructure"
    return "Executive Sponsor"


# ---------------------------------------------------------------------
# LLM call with Retry Logic
# ---------------------------------------------------------------------

_genai_client: Optional[genai.Client] = None

def _ensure_genai_client() -> genai.Client:
    """Return a google-genai Client configured for Vertex AI."""
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("VERTEX_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set for Step 5.")

    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

    _genai_client = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=HttpOptions(
            api_version="v1",
            headers={
                "X-Vertex-AI-LLM-Request-Type": "dedicated",
            },
        ),
    )
    return _genai_client

# --- RETRY HELPER FUNCTION ---
def _is_rate_limit_error(exception: BaseException) -> bool:
    """Checks if the exception is a ClientError with a 429 status code."""
    # This checks the specific Google GenAI ClientError status code
    return isinstance(exception, ClientError) and exception.status_code == 429
# ---------------------------

@retry(
    # Stop after a max of 5 attempts
    stop=stop_after_attempt(5),
    # Wait 2^x seconds between retries (1, 2, 4, 8, 16 seconds)
    wait=wait_exponential(multiplier=1, min=1, max=60),
    # Only retry if the error is a 429 ClientError
    retry=retry_if_exception_type(ClientError) & retry_if_exception(
        _is_rate_limit_error
    ),
    # Print a warning before retrying
    before_sleep=lambda retry_state: log.warning(
        f"[step5] LLM call hit 429, retrying in {int(retry_state.next_action.sleep)}s (Attempt {retry_state.attempt_number} of 5)..."
    ),
    reraise=True # Re-raise the exception if retries are exhausted
)
def _llm_generate_compelling_events(
    company: str,
    alignments: List[Dict[str, Any]],
    problems: List[Dict[str, Any]],
    max_events: int,
    temperature: float = 0.2,
) -> List[Dict[str, Any]]:
    """
    Call the LLM ONCE with a compact JSON context and ask it to propose
    compelling_events.
    """
    if not alignments:
        return []

    # Build a concise, issue-centric context the LLM can reason over.
    problems_by_issue: Dict[str, List[Dict[str, Any]]] = {}
    for p in problems or []:
        if not isinstance(p, dict):
            continue
        issue_id = p.get("issue_id")
        if not issue_id:
            continue
        problems_by_issue.setdefault(issue_id, []).append(p)

    context_payload = {
        "company_name": company,
        "issues": [],
    }

    # Helper: summarize a single problem into a compact JSON-friendly structure
    def summarize_problem(p: Dict[str, Any]) -> Dict[str, Any]:
        evidences = _flatten_sources_from_problem(p)
        snippets: List[str] = []
        for ev in evidences[:3]:  # cap snippets per problem
            if not isinstance(ev, dict):
                continue
            snippet = (ev.get("snippet") or ev.get("title") or "").strip()
            if snippet:
                snippets.append(snippet)
        return {
            "problem_id": p.get("id") or p.get("problem_id") or p.get("issue_id"),
            "label": (p.get("label") or p.get("title") or "").strip(),
            "summary": (p.get("summary") or p.get("description") or "").strip(),
            "evidence_snippets": snippets,
        }

    for a in alignments:
        if not isinstance(a, dict):
            continue
        issue_id = a.get("issue_id")
        issue_problems = problems_by_issue.get(issue_id, [])
        problem_summaries: List[Dict[str, Any]] = [
            summarize_problem(p) for p in issue_problems[:3]  # cap problems per issue
        ]

        issue_ctx = {
            "issue_id": issue_id,
            "issue_title": a.get("issue_title") or a.get("issue_label") or "",
            "aligned_challenges": a.get("aligned_challenges", []),
            "evidence_summary": a.get("evidence_summary") or "",
            "evidence_stats": a.get("evidence_stats") or {},
            # Compact view of upstream problems and their evidence.
            "problems": problem_summaries,
            # Raw alignment is included so the model can see any additional fields
            "raw_alignment": a,
        }
        context_payload["issues"].append(issue_ctx)

    system_instructions = (
        "You are a senior enterprise seller.\n"
        "You are given JSON describing issues that have been aligned to campaign challenges for a single target account. "
        "Each issue may include upstream problems and short evidence snippets from public signals.\n\n"
        "Your job is to propose 3–6 crisp, account-specific compelling events that a seller could actually use in outreach.\n\n"
        "CRITICAL JSON RULES:\n"
        "- You MUST return exactly ONE valid JSON object.\n"
        "- The top-level object MUST have a `compelling_events` key whose value is a list.\n"
        "- Do NOT include any text before or after the JSON object. No explanations, no prose.\n"
        "- Do NOT include comments.\n"
        "- Do NOT insert raw newline characters inside JSON string values; keep each string on a single line, or use \\n if you must break a line.\n"
        "- Do NOT truncate string values; always close string quotes and all brackets/braces.\n\n"
        "Each compelling event MUST:\n"
        "- Be grounded in the issues, problems, evidence_snippets, and aligned challenges you see in the JSON context.\n"
        "- Sound like the *reason to talk now* for this specific account, not a generic industry statement.\n"
        "- Be written in natural language, not bullet fragments.\n"
        "- Avoid generic filler like 'Drift, rework, and value leakage'.\n"
        "- Avoid canned consulting-style headlines like 'Aggressive Expansion Risks a Fragmented Operating Posture' "
        "or similar; instead, write plainly in the customer's language.\n"
    )

    user_prompt = (
        "Return ONLY valid JSON. No markdown, no surrounding prose.\n"
        "If you are unsure what to return, respond with {\"compelling_events\": []}.\n\n"
        "Respond with an object with this structure and concrete values:\n"
        "{\n"
        "  \"compelling_events\": [\n"
        "    {\n"
        "      \"issue_title\": \"short, human-readable summary of the issue in the customer's language\",\n"
        "      \"stakeholder\": \"primary executive or VP who owns this issue (e.g., CIO, CISO, COO)\",\n"
        "      \"event_message\": \"one or two natural-language sentences that clearly describe why this issue is a reason to act now for this specific account\",\n"
        "      \"risk_if_ignored\": \"what bad outcomes are likely if the customer does nothing in the next 12-24 months\",\n"
        "      \"urgency_trigger\": \"what upcoming event, milestone, or trend should make this urgent in the next 1-3 quarters\",\n"
        "      \"opportunity_if_addressed\": \"what upside or strategic advantage the customer captures if they address this issue\",\n"
        "      \"aligned_challenges\": [\"one_or_more_campaign_challenge_keys_here\"]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"Company: {company}\n"
        "You are given a JSON context object (in a separate message part) with a `company_name` and an `issues` list. "
        "Each issue includes aligned campaign challenges, optional problems, and evidence_snippets summarizing public signals.\n"
        "Use those issues, problems, and evidence_snippets to make each event specific and realistic for this account.\n"
        f"Cap the output at {max_events} compelling_events."
    )

    client = _ensure_genai_client()

    temp_env = os.getenv("TA_STEP5_TEMPERATURE") or os.getenv("STEP5_TEMPERATURE")
    max_tokens_env = os.getenv("TA_STEP5_MAX_OUTPUT_TOKENS") or os.getenv("STEP5_MAX_OUTPUT_TOKENS")
    try:
        temp_val = float(temp_env) if temp_env is not None else temperature
    except ValueError:
        temp_val = temperature
    try:
        max_output_tokens = int(max_tokens_env) if max_tokens_env is not None else 4096
    except ValueError:
        max_output_tokens = 4096

    # Combine the JSON context and user instructions into a single user message.
    contents = [
        {
            "role": "user",
            "parts": [
                {"text": json.dumps(context_payload, ensure_ascii=False)},
                {"text": user_prompt},
            ],
        }
    ]

    config = genai_types.GenerateContentConfig(
        system_instruction=system_instructions,
        temperature=temp_val,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
    )

    # API CALL WITH RETRY
    resp = client.models.generate_content(
        model=os.getenv("STEP5_MODEL", "gemini-2.5-pro"),
        contents=contents,
        config=config,
    )

    try:
        raw = getattr(resp, "text", None)
        if not raw and getattr(resp, "candidates", None):
            first = resp.candidates[0]
            if getattr(first, "content", None) and first.content.parts:
                raw = first.content.parts[0].text
        if not raw:
            log.warning("[step5] LLM returned empty response text")
            return []

        # Log full raw LLM output for debugging.
        log.warning("[step5] RAW LLM OUTPUT:\n%s", raw)

        try:
            sanitized = _sanitize_json_like(raw)
            parsed = json.loads(sanitized)
        except json.JSONDecodeError as e:
            log.warning("[step5] LLM JSON parse error (strict): %s", e)
            parsed = _extract_first_json_obj(raw)
            if parsed is None:
                return []

        # Handle a few common shapes:
        # 1) Top-level object with "compelling_events"
        # 2) Top-level list of events (wrap into an object)
        if isinstance(parsed, list):
            events = parsed
        elif isinstance(parsed, dict):
            events = parsed.get("compelling_events", [])
        else:
            events = []

        if not isinstance(events, list):
            # Sometimes the extracted object is a dict containing a nested list under a slightly different key;
            # in that case, bail out rather than guessing.
            return []

        return [e for e in events if isinstance(e, dict)]
    except Exception as e:
        # If any non-ClientError parsing or data handling fails, log and return empty.
        log.warning("[step5] LLM JSON handling failed: %s: %s", type(e).__name__, e)
        return []


# ---------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------

def run_step(
    *,
    problems: List[Dict[str, Any]],
    alignments: List[Dict[str, Any]],
    extra_evidence: Optional[List[Dict[str, Any]]] = None,
    strict: str = "medium",
    max_sources: int = 3,
    company: str = "Unknown Company",
) -> Dict[str, Any]:
    """
    Main entrypoint used from run.py

    - Uses alignments from Step 4 as the backbone.
    - Uses problems from Step 2 as the evidence reservoir.
    - Calls LLM once to generate human text for each event.
    - Post-processes with deterministic scoring + evidence_stats.
    """

    extra_evidence = extra_evidence or []
    problems = [p for p in (problems or []) if isinstance(p, dict)]
    alignments = [a for a in (alignments or []) if isinstance(a, dict)]

    log.info(f"[step5] run_step company={company!r} alignments={len(alignments)} problems={len(problems)}")

    if not alignments:
        return {
            "step": "compelling_events",
            "company_name": company,
            "generated_at": _now_iso(),
            "compelling_events": [],
        }

    # 1) Ask LLM to draft event text based purely on alignments + challenges
    #    (no confidence or source wiring yet)
    max_events = min(max(len(alignments) * 2, 3), 8)

    try:
        llm_events = _llm_generate_compelling_events(
            company=company,
            alignments=alignments,
            problems=problems,
            max_events=max_events,
            temperature=float(os.getenv("STEP5_TEMPERATURE", "0.4")),
        )
    except ClientError as e:
        # This catches ClientError (including 429) after all retries have failed
        log.error(f"[step5] LLM generation failed after retries (API Error): {e}")
        llm_events = []
    except Exception as e:
        log.error(f"[step5] LLM generation failed due to unexpected error: {e}")
        llm_events = []

    # If the LLM did not return any compelling_events, emit a diagnostic meta-event
    # instead of an empty list so callers can see that the gap is a signal, not a bug.
    if not llm_events:
        log.info(
            "[step5] LLM returned 0 compelling_events; emitting diagnostic meta-event "
            "to indicate weak campaign–account alignment or LLM failure."
        )
        diagnostic_event = {
            "issue_title": "No strong campaign-aligned compelling events detected",
            "stakeholder": "Executive Sponsor",
            "event_message": (
                "Based on the current issues and campaign taxonomy, no strong, campaign-aligned "
                "compelling events were detected for this account. This likely indicates a weak "
                "fit between the campaign focus and the account's most visible public challenges "
                "or an upstream LLM failure. Check logs for API errors."
            ),
            "risk_if_ignored": (
                "Continuing to invest effort in this campaign for this account without revisiting "
                "the value hypothesis may lead to low response rates and wasted seller cycles."
            ),
            "urgency_trigger": (
                "Treat this as a trigger to re-evaluate whether this account belongs in the current "
                "campaign or whether the campaign challenge taxonomy needs to be updated to reflect "
                "real-world signals."
            ),
            "opportunity_if_addressed": (
                "By tightening campaign–account fit or updating the challenge taxonomy, you can focus "
                "effort where external signals show clearer pain or opportunity."
            ),
            "aligned_challenges": [],
        }
        llm_events = [diagnostic_event]

    # 2) For each LLM event, attach evidence and scoring
    compelling_events: List[Dict[str, Any]] = []
    for raw_ev in llm_events:
        issue_title = (raw_ev.get("issue_title") or "").strip()
        aligned_challenges = raw_ev.get("aligned_challenges") or []

        # Find the best matching alignment by title overlap
        best_align: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for a in alignments:
            atitle = (a.get("issue_title") or a.get("issue_label") or "").strip()
            if not atitle:
                continue
            overlap = len(set(issue_title.lower().split()) & set(atitle.lower().split()))
            if overlap > best_score:
                best_score = overlap
                best_align = a

        issue_id = best_align.get("issue_id") if best_align else None
        # Evidence: all problem evidence tied to this issue_id
        issue_evidence = _collect_issue_evidence(problems, issue_id=issue_id)

        # Optionally mix in a few extra web events (if they’ve been normalized with issue_id)
        # For now, we treat extra_evidence as global and rely on problems for specificity.
        sources = _merge_sources(issue_evidence, [], max_total=max_sources)

        total_sources = len(sources)
        # Use issue_specific_sources calculation from best_align if available, otherwise default to total
        if best_align and isinstance(best_align, dict):
            issue_specific_sources = (
                best_align.get("evidence_stats", {}).get("issue_specific_sources")
            )
        else:
            issue_specific_sources = None
        if issue_specific_sources is None:
             issue_specific_sources = len(issue_evidence) if issue_evidence else total_sources

        recency_score = _score_evidence_recency(sources)
        confidence = _score_confidence(
            issue_specific_sources=issue_specific_sources,
            total_sources=total_sources,
            recency_score=recency_score,
            strict_level=strict,
        )

        # Stakeholder guess if LLM didn’t give a good one
        stakeholder = (raw_ev.get("stakeholder") or "").strip()
        if not stakeholder:
            stakeholder = _default_stakeholder_from_challenges(aligned_challenges)

        compelling_events.append(
            {
                "sources": sources,
                "confidence": confidence,
                "issue_title": issue_title or (best_align.get("issue_title") if best_align else ""),
                "stakeholder": stakeholder,
                "event_message": (raw_ev.get("event_message") or "").strip(),
                "risk_if_ignored": (raw_ev.get("risk_if_ignored") or "").strip(),
                "urgency_trigger": (raw_ev.get("urgency_trigger") or "").strip(),
                "opportunity_if_addressed": (raw_ev.get("opportunity_if_addressed") or "").strip(),
                "aligned_challenges": aligned_challenges or (best_align.get("aligned_challenges") if best_align else []),
                "evidence_stats": {
                    "total_sources": total_sources,
                    "issue_specific_sources": issue_specific_sources,
                },
            }
        )

    # 3) Sort events by confidence descending and truncate if we got too many
    compelling_events.sort(key=lambda e: e.get("confidence", 0.0), reverse=True)

    max_final = int(os.getenv("STEP5_MAX_EVENTS", "6"))
    compelling_events = compelling_events[:max_final]

    return {
        "step": "compelling_events",
        "company_name": company,
        "generated_at": _now_iso(),
        "compelling_events": compelling_events,
    }


if __name__ == "__main__":
    # Simple CLI wrapper for testing
    import argparse

    parser = argparse.ArgumentParser(description="Step 5 – Compelling Events")
    # Support both new and legacy flags:
    parser.add_argument(
        "--problems",
        help="Path to Step 2 problems JSON (preferred; alias of --issues)",
    )
    parser.add_argument(
        "--issues",
        help="Legacy: path to Step 2 problems JSON (alias of --problems)",
    )
    parser.add_argument("--alignments", required=True, help="Path to Step 4 alignments JSON")
    parser.add_argument("--evidence", help="Optional path to Step 1 evidence JSON")
    parser.add_argument("--company", default="Unknown Company")
    parser.add_argument("--strict", default="medium")
    parser.add_argument("--max-sources", type=int, default=3)
    parser.add_argument("--out-json", required=True)
    args = parser.parse_args()

    # Backward-compatible resolution of the problems/issues path
    problems_path = args.problems or args.issues
    if not problems_path:
        raise SystemExit("ERROR: You must provide either --problems or --issues pointing to Step 2 output JSON.")

    with open(problems_path, "r", encoding="utf-8") as fh:
        problems_obj = json.load(fh)

    with open(args.alignments, "r", encoding="utf-8") as fh:
        align_obj = json.load(fh)
    evidence_obj = []
    if args.evidence:
        try:
            with open(args.evidence, "r", encoding="utf-8") as fh:
                evidence_obj = json.load(fh)
        except Exception:
            evidence_obj = []

    # step2/4 wrappers: they usually store under top-level keys
    problems_list = problems_obj.get("problems", problems_obj) if isinstance(problems_obj, dict) else problems_obj
    align_list = align_obj.get("alignments", align_obj) if isinstance(align_obj, dict) else align_obj
    ev_list = evidence_obj if isinstance(evidence_obj, list) else evidence_obj.get("results", [])

    out = run_step(
        problems=problems_list,
        alignments=align_list,
        extra_evidence=ev_list,
        strict=args.strict,
        max_sources=args.max_sources,
        company=args.company,
    )

    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)

    # Human-readable log to stderr
    print(f"[Step 5] Wrote compelling events JSON \u2192 {args.out_json}", file=sys.stderr)

    # Machine-readable markers for orchestrators (stdout)
    print(f"__STEP5_JSON_PATH__:{args.out_json}")
    print(f"__STEP5_EVENT_COUNT__:{len(out.get('compelling_events') or [])}")