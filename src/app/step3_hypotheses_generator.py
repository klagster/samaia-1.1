#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 3: Hypotheses Generator (session-aware)
--------------------------------------------
Reads the normalized evidence index (from step2_evidence_harvester.py), clusters
signals, and generates evidenced business problems (hypotheses) that conform to
the SAMaiA "problems" schema used by run.py (Step 3 output shape).

Updates in this patch:
- **LLM Retry Logic**: Implemented exponential backoff and retries for Gemini API calls
  to handle transient 429 RESOURCE_EXHAUSTED errors.
- **Session-aware**: accepts `--session-dir` and auto-discovers inputs/outputs.
- **Orchestrator markers** printed to STDOUT for `run.py` to scrape.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

# --- NEW IMPORTS FOR RETRY LOGIC ---
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception
from google.genai.errors import ClientError
# -----------------------------------

from google import genai
from google.genai import types as genai_types
from google.genai.types import HttpOptions

# ----------------------------
# Utilities
# ----------------------------

ISO_NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_ndjson(path: str | Path) -> dict:
    """Load NDJSON records and coerce into evidence index shape.
    Expected each line to be a JSON object representing one evidence item.
    """
    items: List[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    items.append(obj)
            except Exception:
                continue
    return {"evidence": items}


def _is_http(url: str | None) -> bool:
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))


def _parse_date(d: str | None) -> Optional[datetime]:
    if not d or not isinstance(d, str):
        return None
    try:
        if len(d) >= 10 and d[4] == "-" and d[7] == "-":
            return datetime.fromisoformat(d[:10] + "T00:00:00+00:00")
        return datetime.fromisoformat(d)
    except Exception:
        return None


def _days_ago(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _domain(host: str | None) -> str:
    if not host:
        return "unknown"
    h = host.lower()
    h = re.sub(r"^(www|news|media|press|ir|investors|newsroom)\.", "", h)
    return h


def _extract_host(url: str | None) -> str:
    if not _is_http(url):
        return "unknown"
    try:
        return _domain(re.sub(r"^https?://", "", url).split("/")[0])
    except Exception:
        return "unknown"


# ----------------------------
# Evidence normalization helper
# ----------------------------

from typing import Any

def _normalize_evidence_entries(evidence_raw: Any) -> List[dict]:
    """Normalize LLM-provided evidence into the canonical dict shape expected downstream.

    Canonical shape:
      {
        "source": str,
        "url_or_id": str,
        "date": "YYYY-MM-DD" or "",
        "quote_or_note": str,
      }

    This function is resilient to:
    - Bare string evidence entries (treated as quote_or_note)
    - Partially structured dicts with fields like url/url_or_id/date/date_iso/snippet/title
    """
    normalized: List[dict] = []
    if not evidence_raw:
        return normalized

    for ev in evidence_raw:
        # Case 1: bare string → wrap as quote_or_note only.
        if isinstance(ev, str):
            text = ev.strip()
            if not text:
                continue
            normalized.append(
                {
                    "source": "LLM-derived",
                    "url_or_id": "",
                    "date": "",
                    "quote_or_note": text[:280],
                }
            )
            continue

        # Case 2: dict-like evidence → normalize common fields.
        if isinstance(ev, dict):
            url = ev.get("url") or ev.get("url_or_id") or ""
            date_raw = ev.get("date") or ev.get("date_iso") or ""
            date_norm = (date_raw or "")[:10]

            quote = (
                ev.get("quote_or_note")
                or ev.get("snippet")
                or ev.get("text")
                or ev.get("title")
                or ""
            )
            quote = (quote or "").strip()
            if not quote and not url:
                # Nothing meaningful to index.
                continue

            source = (
                ev.get("source")
                or ev.get("publisher")
                or _extract_host(url)
                or "Unknown"
            )

            normalized.append(
                {
                    "source": source,
                    "url_or_id": url,
                    "date": date_norm,
                    "quote_or_note": quote[:280],
                }
            )
            continue

        # Unknown structure → skip.
        continue

    return normalized

# ----------------------------
# Heuristics / Taxonomy
# ----------------------------

CATEGORY_MAP = {
    "Financial Filings & Capital Markets": "Growth",
    "Corporate & Official Sources": "Operations",
    "Jobs & Hiring Signals": "Operations",
    "Risk, Compliance & Security": "Risk",
    "Technology & Operations (General)": "Operations",
    "Technology & Operations (Data Center / AI)": "Growth",
    "Sustainability & Energy": "Cost",
    "Market, News & Analyst Reports": "Growth",
    "Customer & Product": "CX",
    "M&A, Geography & Expansion": "Growth",
}

SOURCE_QUALITY_PRIOR = {
    # Official
    "sec.gov": 1.0, "europa.eu": 0.95, "ftc.gov": 0.95, "ico.org.uk": 0.95, "cnil.fr": 0.95,
    # Tier-1 media / wires
    "reuters.com": 0.95, "bloomberg.com": 0.95, "businesswire.com": 0.9, "prnewswire.com": 0.9, "globenewswire.com": 0.9,
    # Generic baseline
    "default": 0.7,
}


KPI_BY_PROB_CAT = {
    "Growth": ["Revenue growth", "Win rate", "Time-to-market", "CapEx efficiency"],
    "Cost": ["Operating margin", "Unit cost", "Energy cost per kWh", "PUE"],
    "Risk": ["# security incidents", "MTTD/MTTR", "Regulatory fines", "Audit pass rate"],
    "Operations": ["Change failure rate", "Deployment lead time", "SLA/SLO attainment", "Service availability"],
    "CX": ["NPS/CSAT", "Churn", "Time-to-value", "Support ticket volume"],
    "Talent": ["Time-to-hire", "Open reqs", "Attrition"],
}


_genai_client: Optional[genai.Client] = None



def _map_problem_category(ev_cat: str) -> str:
    return CATEGORY_MAP.get(ev_cat, "Operations")


def _urgency_from_days(days: Optional[int]) -> Tuple[str, str]:
    if days is None:
        return "Unknown", "Unknown"
    if days <= 90:
        return "High", "0–6m"
    if days <= 365:
        return "Medium", "6–18m"
    return "Low", "18m+"


def _score_confidence(source_q: float, days: Optional[int], count: int) -> float:
    recency = 1.0
    if days is not None:
        recency = max(0.2, 1.0 - (max(0, days - 90) / 900.0))
    multiplicity = min(1.0, 0.35 + math.log2(max(1, count)) / 4.0)
    raw = 0.35 * source_q + 0.40 * recency + 0.25 * multiplicity
    return round(max(0.05, min(0.98, raw)), 2)


def _source_quality_from_url(url: str) -> float:
    h = _extract_host(url)
    return SOURCE_QUALITY_PRIOR.get(h, SOURCE_QUALITY_PRIOR["default"])


def _collect_topic_terms(texts: List[str]) -> List[str]:
    bags = Counter()
    for t in texts:
        if not isinstance(t, str):
            continue
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", t.lower())
        for tok in tokens:
            if tok in {"the","and","for","with","into","from","that","this","have","has","are","was","were","will","can","not","but","you","our","their","about","over","under","between"}:
                continue
            bags[tok] += 1
    return [w for w,_ in bags.most_common(8)]

# --- JSON Extraction Helper ---
def _extract_first_json_obj(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of the first top-level JSON object from a string."""
    if not text or not isinstance(text, str):
        return None

    # Strip markdown fences
    t = text.strip()
    if t.startswith("```"):
        t = "\n".join(t.splitlines()[1:-1] if t.endswith("```") else t.splitlines()[1:])

    start = t.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(t[start:], start=start):
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
                        candidate = t[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            # Failed to parse, break inner loop and search for next {
                            break
        start = t.find("{", start + 1)
    return None
# ----------------------------


# ----------------------------
# LLM integration (google-genai / ADK style)
# ----------------------------

def _ensure_genai_client() -> genai.Client:
    """Return a google-genai Client configured for Vertex AI.

    This step expects VERTEX_PROJECT and VERTEX_LOCATION to be provided via
    environment variables (they are set by the Cloud Functions/Run deploy
    Makefile). No API keys are used; ADC is assumed.
    """
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("VERTEX_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set for Step 3.")

    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

    _genai_client = genai.Client(
        # GOOGLE_GENAI_USE_VERTEXAI=True in env also works, but this is explicit:
        vertexai=True,
        project=project,
        location=location,
        http_options=HttpOptions(
            api_version="v1",
            headers={
                # "shared"  -> standard pay-as-you-go
                # "dedicated" -> use provisioned throughput (must pair with PT endpoint model)
                "X-Vertex-AI-LLM-Request-Type": "dedicated",
            },
        ),
    )
    return _genai_client

# --- RETRY HELPER FUNCTION ---
def _is_rate_limit_error(exception: BaseException) -> bool:
    """Checks if the exception is a ClientError with a 429 status code."""
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
    before_sleep=lambda retry_state: print(
        f"[Step 3] LLM call hit 429, retrying in {int(retry_state.next_action.sleep)}s (Attempt {retry_state.attempt_number} of 5)...",
        file=sys.stderr,
    ),
    reraise=True # Re-raise the exception if retries are exhausted
)
def _llm_generate_hypotheses(
    *,
    evidence_index: dict,
    company: str,
    time_window: str,
    max_per_bucket: int = 3,
) -> dict:
    """Generate evidenced problems/hypotheses using Gemini (google-genai).

    The LLM is responsible for:
    - Grouping related evidence into problems
    - Creating clear, non-templated titles and "why it matters" fields
    - Filling the problems array in the same schema as build_hypotheses

    We keep build_hypotheses as a deterministic fallback if the LLM fails.
    """
    client = _ensure_genai_client()

    # To avoid overloading the model, we limit and lightly pre-structure evidence.
    ev_items = evidence_index.get("evidence") or []
    MAX_ITEMS = 120
    trimmed_items = ev_items[:MAX_ITEMS]

    structured_evidence = []
    for idx, ev in enumerate(trimmed_items):
        if not isinstance(ev, dict):
            continue
        url = ev.get("url") or ev.get("url_or_id") or ""
        if not _is_http(url):
            continue
        structured_evidence.append(
            {
                "id": idx,
                "category": ev.get("category") or ev.get("cat") or "Uncategorized",
                "publisher": ev.get("publisher") or ev.get("source") or _extract_host(url),
                "url": url,
                "title": ev.get("title") or "",
                "snippet": ev.get("snippet") or ev.get("quote_or_note") or ev.get("raw_excerpt") or "",
                "date_iso": ev.get("date_iso") or ev.get("date") or "",
            }
        )

    system_prompt = (
        "You are an expert B2B strategist preparing executive-ready, evidenced business problem hypotheses for a GTM campaign.\n\n"
        "GOALS:\n"
        "1) Synthesize a SMALL set (3–10) of the most material, non-overlapping business problems for the specified company.\n"
        "2) Ground every problem DIRECTLY in the provided evidence. Do not invent facts, companies, or events.\n"
        "3) Write for executive stakeholders (CIO, CISO, COO, CFO, CHRO, CMO) in clear, concrete language.\n\n"
        "STYLE CONSTRAINTS:\n"
        "- Avoid templated or consulting-jargon titles such as 'Aggressive Expansion Risks a Fragmented Operating Posture'.\n"
        "- Prefer plain, specific titles that reflect the actual situation (e.g., 'Cloud ERP rollout delays are driving service-level misses in North America').\n"
        "- Use concise but RICH narrative (3–6 sentences) in why_it_matters, focusing on impact, risk, and urgency.\n"
        "- Do NOT copy evidence text verbatim; summarize and synthesize.\n\n"
        "OUTPUT FORMAT:\n"
        "- Return ONLY a single JSON object with fields: company, generated_at, time_window, problems.\n"
        "- problems MUST be an array of objects with this exact schema:\n"
        "  {title, category, why_it_matters, primary_stakeholder, secondary_stakeholders, evidence_refs, artifact_refs,\n"
        "   affected_kpis, competitive_context, urgency, impact, confidence, blind_spots,\n"
        "   validation_questions, value_prop_mapping}.\n"
        "- urgency MUST be an object: {level, time_horizon}.\n"
        "- Do NOT fabricate evidence text or URLs. For each problem, return an evidence_refs array listing integer IDs from evidence_items.\n"
        "- Each entry of evidence_refs must be an integer id field from the provided evidence_items; do not create new IDs.\n"
        "- primary_stakeholder should normally be a C-level role (CIO, CISO, COO, CFO, CHRO, CMO) or BU leader.\n"
        "- secondary_stakeholders should list 1–4 adjacent functions likely impacted (e.g., 'IT operations', 'Security engineering').\n"
        "- affected_kpis should list 3–8 KPIs that would move if this problem is solved.\n"
        "- competitive_context should describe how competitors or substitutes gain advantage if this issue is not addressed.\n"
        "- blind_spots should list 2–4 important unknowns or assumptions.\n"
        "- validation_questions should list 3–6 concrete questions a salesperson or advisor could ask to confirm the problem.\n\n"
        "CRITICAL RULES:\n"
        "- All claims must be traceable to the provided evidence or be moderate, generic business inferences.\n"
        "- Do not reference internal model behavior, prompts, or JSON itself in the output.\n"
        "- Do not include any text outside the JSON object (no explanations, no commentary).\n"
    )

    user_payload = {
        "company": company,
        "time_window": time_window,
        "evidence_items": structured_evidence,
        "kpi_by_problem_category": KPI_BY_PROB_CAT,
        "max_problems": 10,
    }

    temp_env = os.getenv("TA_STEP3_TEMPERATURE") or os.getenv("STEP3_TEMPERATURE")
    max_tokens_env = os.getenv("TA_STEP3_MAX_OUTPUT_TOKENS") or os.getenv("STEP3_MAX_OUTPUT_TOKENS")
    try:
        temp_val = float(temp_env) if temp_env is not None else 0.55
    except ValueError:
        temp_val = 0.55
    try:
        max_output_tokens = int(max_tokens_env) if max_tokens_env is not None else 4096
    except ValueError:
        max_output_tokens = 4096

    contents = [
        {
            "role": "user",
            "parts": [
                {"text": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
    ]

    gen_config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=temp_val,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
    )

    # API CALL WITH RETRY
    resp = client.models.generate_content(
        model=os.getenv("STEP3_MODEL", "gemini-2.5-pro"),
        contents=contents,
        config=gen_config,
    )

    try:
        text = resp.text if hasattr(resp, "text") else str(resp)
        # Attempt strict load first
        obj = json.loads(text)
    except Exception:
        # If strict load fails, attempt robust extraction
        obj = _extract_first_json_obj(text)
        if obj is None:
            # Fallback to deterministic behavior if parsing fails.
            return build_hypotheses(
                evidence_index=evidence_index,
                company=company,
                time_window=time_window,
                max_per_bucket=max_per_bucket,
            )


    problems = obj.get("problems") if isinstance(obj, dict) else None
    if not isinstance(problems, list):
        # If the model didn't follow the contract, fall back.
        return build_hypotheses(
            evidence_index=evidence_index,
            company=company,
            time_window=time_window,
            max_per_bucket=max_per_bucket,
        )

    # Build evidence_lookup: Dict[int, dict] mapping evidence IDs to entries.
    evidence_lookup: Dict[int, dict] = {}
    for item in structured_evidence:
        try:
            evid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        evidence_lookup[evid] = item

    # Normalize problems and attach evidence from Step 1/2 via evidence_refs IDs.
    normalized_problems: List[dict] = []
    for p in problems:
        if not isinstance(p, dict):
            continue

        refs = p.get("evidence_refs") or []
        if not isinstance(refs, list):
            refs = []

        evidence_arr: List[dict] = []
        for rid in refs:
            try:
                evid = int(rid)
            except (TypeError, ValueError):
                continue
            item = evidence_lookup.get(evid)
            if not item:
                continue

            url = item.get("url") or ""
            quote = item.get("snippet") or item.get("title") or ""
            evidence_arr.append(
                {
                    "source": item.get("publisher") or _extract_host(url),
                    "url_or_id": url,
                    "date": (item.get("date_iso") or "")[:10],
                    "quote_or_note": (quote or "")[:280],
                }
            )

        # Attach the canonical evidence array; keep evidence_refs for debugging if desired.
        p["evidence"] = evidence_arr

        normalized_problems.append(p)

    return {
        "company": obj.get("company") or company,
        "generated_at": obj.get("generated_at") or ISO_NOW,
        "time_window": obj.get("time_window") or time_window,
        "problems": normalized_problems,
    }

# ----------------------------
# Core
# ----------------------------


def build_hypotheses(evidence_index: dict, company: str, time_window: str, max_per_bucket: int = 3) -> dict:
    ev_items = evidence_index.get("evidence") or []
    by_cat: Dict[str, List[dict]] = defaultdict(list)
    for ev in ev_items:
        if not isinstance(ev, dict):
            continue
        url = ev.get("url") or ev.get("url_or_id") or ""
        if not _is_http(url):
            continue
        # Normalize fields we rely on downstream
        ev_norm = {
            "category": ev.get("category") or ev.get("cat") or "Uncategorized",
            "publisher": ev.get("publisher") or ev.get("source") or _extract_host(url),
            "url": url,
            "title": ev.get("title") or "",
            "snippet": ev.get("snippet") or ev.get("quote_or_note") or ev.get("raw_excerpt") or "",
            "date_iso": ev.get("date_iso") or ev.get("date") or "",
        }
        by_cat[ev_norm["category"]].append(ev_norm)

    problems: List[dict] = []

    for ev_cat in sorted(by_cat.keys()):
        bucket = by_cat[ev_cat]
        if not bucket:
            continue

        bucket.sort(key=lambda e: (
            _days_ago(_parse_date(e.get("date_iso") or "")) or 10**6,
            -_source_quality_from_url(e.get("url")),
        ))

        for i in range(0, min(len(bucket), max_per_bucket * 2), max_per_bucket):
            chunk = bucket[i:i + max_per_bucket]
            if not chunk:
                continue

            category_mapped = _map_problem_category(ev_cat)
            kpis = KPI_BY_PROB_CAT.get(category_mapped, [])[:4]

            top_date = _parse_date((chunk[0].get("date_iso") or ""))
            days = _days_ago(top_date)

            # Title will be computed below based on top_terms.

            evidence_arr = []
            texts_for_topics = []
            for c in chunk:
                url = c.get("url")
                if not _is_http(url):
                    continue
                quote = c.get("snippet") or c.get("title") or ""
                evidence_arr.append({
                    "source": c.get("publisher") or _extract_host(url),
                    "url_or_id": url,
                    "date": (c.get("date_iso") or "")[:10],
                    "quote_or_note": (quote or "")[:280],
                })
                texts_for_topics.append(quote)

            if not evidence_arr:
                continue

            avg_sq = sum(_source_quality_from_url(e["url_or_id"]) for e in evidence_arr) / max(1, len(evidence_arr))
            conf = _score_confidence(avg_sq, days, len(evidence_arr))

            urgency_level, time_hz = _urgency_from_days(days)
            impact = "High" if conf >= 0.7 else ("Medium" if conf >= 0.45 else "Low")

            top_terms = _collect_topic_terms(texts_for_topics)[:5]

            if top_terms:
                # Data-driven, plain-language title derived from the most frequent terms.
                # Example: "Growth pressure around capacity, latency, SLAs"
                title_terms = ", ".join(top_terms[:3])
                title = f"{category_mapped} pressure around {title_terms}"
                why = (
                    f"Observed signals point to {category_mapped.lower()} pressure around: "
                    + ", ".join(top_terms)
                )
            else:
                # Fallback to a neutral, non-jargony title if we cannot extract terms.
                title = f"Evidenced {category_mapped} issue"
                why = f"Observed signals indicate {category_mapped.lower()} considerations."

            blind_spots = [
                "Exact scope and budget of the initiative are not stated.",
                "Internal success metrics and timelines are not disclosed.",
            ]
            validations = [
                "Confirm scope (regions, products, or assets) implicated by these signals.",
                "Request KPIs and milestones tied to this initiative.",
            ]

            problems.append({
                "title": title,
                "category": category_mapped,
                "why_it_matters": why,
                "primary_stakeholder": "Unknown",
                "secondary_stakeholders": [],
                "evidence": evidence_arr,
                "artifact_refs": [],
                "affected_kpis": kpis,
                "competitive_context": "Unknown",
                "urgency": {"level": urgency_level, "time_horizon": time_hz},
                "impact": impact,
                "confidence": conf,
                "blind_spots": blind_spots,
                "validation_questions": validations,
                "value_prop_mapping": "Unknown",
            })

    return {
        "company": company,
        "generated_at": ISO_NOW,
        "time_window": time_window,
        "problems": problems,
    }



# In-process callable used by run.py to avoid subprocess + disk I/O
# Now prefers LLM (Vertex/Gemini) with deterministic fallback.
def run_step(*, evidence_index: dict, company: str, time_window: str, max_per_bucket: int = 3) -> dict:
    """Run Step 3 using Gemini (Vertex) with deterministic fallback.

    Preferred path: use _llm_generate_hypotheses for creative, evidence-grounded
    problem synthesis. If Vertex is not configured or the call fails, fall
    back to build_hypotheses for deterministic behavior.
    """
    try:
        return _llm_generate_hypotheses(
            evidence_index=evidence_index,
            company=company,
            time_window=time_window,
            max_per_bucket=max_per_bucket,
        )
    except ClientError as e: # <-- Catch the specific API error (including 429) after retries fail
        print(f"[Step 3] LLM generation failed after retries (API Error), falling back to deterministic hypotheses: {e}", file=sys.stderr)
        return build_hypotheses(
            evidence_index=evidence_index,
            company=company,
            time_window=time_window,
            max_per_bucket=max_per_bucket,
        )
    except Exception as e:
        print(f"[Step 3] LLM generation failed due to configuration or unexpected error, falling back to deterministic hypotheses: {e}", file=sys.stderr)
        return build_hypotheses(
            evidence_index=evidence_index,
            company=company,
            time_window=time_window,
            max_per_bucket=max_per_bucket,
        )


def render_markdown(payload: dict) -> str:
    lines = []
    lines.append(f"# Step 3: Evidenced Hypotheses for {payload.get('company','Unknown')}")
    lines.append(f"_Generated at: {payload.get('generated_at','')}; Time window: {payload.get('time_window','')}_\n")
    problems = payload.get("problems") or []
    if not problems:
        lines.append("> No evidenced hypotheses generated from the provided evidence index.")
        return "\n".join(lines)

    for i, p in enumerate(problems, 1):
        lines.append(f"## {i}. {p.get('title','Untitled')}  \n")
        lines.append(
            f"**Category:** {p.get('category','Unknown')}  \n"
            f"**Impact:** {p.get('impact','Unknown')}  \n"
            f"**Urgency:** {p.get('urgency',{}).get('level','Unknown')} "
            f"({p.get('urgency',{}).get('time_horizon','Unknown')})  \n"
            f"**Confidence:** {p.get('confidence',0):.2f}\n"
        )
        lines.append(f"**Why it matters:** {p.get('why_it_matters','')}\n")
        kpis = p.get("affected_kpis") or []
        if kpis:
            lines.append("**Affected KPIs:** " + ", ".join(kpis) + "\n")
        lines.append("**Sources & Citations:**")
        evs = p.get("evidence") or []
        for ev in evs:
            nm = ev.get("source", "Source")
            url = ev.get("url_or_id", "")
            dt = ev.get("date", "")
            qt = (ev.get("quote_or_note", "").strip())
            qt = (qt[:180] + "…") if len(qt) > 180 else qt
            if _is_http(url):
                lines.append(f"- [{nm}]({url}) — {qt} ({dt})")
            else:
                lines.append(f"- {nm}: {qt} ({dt})")
        lines.append("")
    return "\n".join(lines)


def _discover_inputs_outputs(args: argparse.Namespace) -> tuple[Path, Path, Optional[Path]]:
    """Determine evidence input and output paths based on CLI flags/session-dir."""
    session_dir = Path(args.session_dir) if args.session_dir else None

    # Input resolution order: --evidence, session JSON, session NDJSON
    if args.evidence:
        evidence_path = Path(args.evidence)
        if not evidence_path.exists():
            raise FileNotFoundError(f"Evidence not found: {evidence_path}")
    elif session_dir:
        cand_json = session_dir / "evidence.step2.json"
        cand_ndj = session_dir / "evidence.step2.ndjson"
        if cand_json.exists():
            evidence_path = cand_json
        elif cand_ndj.exists():
            evidence_path = cand_ndj
        else:
            raise FileNotFoundError(
                f"No evidence file found in session dir: {session_dir} (looked for evidence.step2.json / evidence.step2.ndjson)"
            )
    else:
        raise ValueError("Provide --evidence or --session-dir for input discovery.")

    # Output JSON
    if args.out_json:
        out_json = Path(args.out_json)
    elif session_dir:
        out_json = session_dir / "hypotheses.step3.json"
    else:
        raise ValueError("Provide --out-json or --session-dir to determine output path.")

    # Output MD (optional): from --out-md or --emit-md flag
    out_md: Optional[Path] = None
    if args.out_md:
        out_md = Path(args.out_md)
    elif session_dir and args.emit_md:
        out_md = session_dir / "hypotheses.step3.md"

    return evidence_path, out_json, out_md


def main():
    ap = argparse.ArgumentParser(description="Step 3 Hypotheses Generator (deterministic, evidence-first, session-aware)")
    ap.add_argument("--evidence", required=False, help="Path to evidence_index.json (or NDJSON) from Step 2 harvester")
    ap.add_argument("--company", required=False, default=None, help="Company name (overrides evidence header)")
    ap.add_argument("--time-window", required=False, default="last 12–18 months")
    ap.add_argument("--out-json", required=False, help="Output JSON path for hypotheses")
    ap.add_argument("--out-md", required=False, help="Optional Markdown summary path")
    ap.add_argument("--max-per-bucket", type=int, default=3, help="Max evidence items per hypothesis")
    ap.add_argument("--session-dir", required=False, help="Session directory to discover inputs/outputs and write artifacts")
    ap.add_argument("--emit-md", action="store_true", help="When using --session-dir, also emit a Markdown summary next to JSON")
    args = ap.parse_args()

    # Discover paths based on args/session
    try:
        evidence_path, out_json, out_md = _discover_inputs_outputs(args)
    except Exception as e:
        print(f"[Step 3] Input/Output resolution error: {e}", file=sys.stderr)
        sys.exit(2)

    # Load evidence (JSON or NDJSON)
    try:
        if evidence_path.suffix.lower() in {".ndjson", ".ndj"}:
            evidence_index = _load_ndjson(evidence_path)
        else:
            evidence_index = _load_json(evidence_path)
    except Exception as e:
        print(f"[Step 3] Failed to load evidence: {e}", file=sys.stderr)
        sys.exit(2)

    company = args.company or evidence_index.get("company") or "Unknown"

    payload = run_step(
        evidence_index=evidence_index,
        company=company,
        time_window=args.time_window,
        max_per_bucket=max(1, args.max_per_bucket),
    )

    # Write JSON
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"[Step 3] Wrote hypotheses JSON → {out_json}", file=sys.stderr)

    # Optional Markdown
    if out_md:
        md = render_markdown(payload)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        with open(out_md, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"[Step 3] Wrote Markdown summary → {out_md}", file=sys.stderr)

    # Emit orchestrator markers for run.py (stdout)
    print(f"__STEP3_JSON_PATH__:{out_json}")
    if out_md:
        print(f"__STEP3_MD_PATH__:{out_md}")
    print(f"__STEP3_PROBLEM_COUNT__:{len(payload.get('problems') or [])}")


if __name__ == "__main__":
    main()