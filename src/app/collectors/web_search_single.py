import json
import logging
import os
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)

__all__ = ["collect_web_events_for_company"]


def _load_query_pack(path: str) -> Dict[str, Any]:
    """Load query pack JSON file."""
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load query pack from {path}: {e}")
    return {"categories": [], "defaults": {"recency_days": 540, "max_results": 10}}


def _build_queries(query_pack: Dict[str, Any], company_name: str, domain: Optional[str]) -> List[str]:
    """Build search queries from query pack."""
    queries = []
    categories = query_pack.get("categories", [])
    
    # Prioritize categories - focus on news, risks, and market signals
    priority_categories = [
        "Market, News & Analyst Reports",
        "Risk, Compliance & Security",
        "Financial Filings & Capital Markets",
        "Technology & Operations (General)",
        "M&A, Geography & Expansion",
    ]
    
    # First, try priority categories
    for category in categories:
        cat_name = category.get("name", "")
        if cat_name not in priority_categories:
            continue
            
        category_queries = category.get("queries", [])
        for query_template in category_queries[:2]:  # Max 2 queries per category
            # Skip queries that require domain if we don't have one
            if "{{domain}}" in query_template and not domain:
                continue
            
            # Replace placeholders
            query = query_template.replace("{{company}}", f'"{company_name}"')
            if domain:
                query = query.replace("{{domain}}", domain)
            
            queries.append(query)
            
            # Limit total queries
            if len(queries) >= 12:
                break
        
        if len(queries) >= 12:
            break
    
    # If we still need more queries, add from other categories
    if len(queries) < 8:
        for category in categories:
            cat_name = category.get("name", "")
            if cat_name in priority_categories:
                continue  # Already processed
                
            category_queries = category.get("queries", [])
            for query_template in category_queries[:1]:  # Just 1 from each
                # Skip queries that require domain if we don't have one
                if "{{domain}}" in query_template and not domain:
                    continue
                
                # Replace placeholders
                query = query_template.replace("{{company}}", f'"{company_name}"')
                if domain:
                    query = query.replace("{{domain}}", domain)
                
                queries.append(query)
                
                if len(queries) >= 12:
                    break
            
            if len(queries) >= 12:
                break
    
    return queries[:12]  # Return max 12 queries


def _execute_vertex_search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """
    Execute web search using Vertex AI's Google Search via the google.genai SDK.
    Uses the same approach as preview_queries.py - asks for structured JSON response.
    """
    try:
        from google import genai
        from google.auth import default as google_auth_default
        import re
        
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "portend-sam")
        location = os.getenv("VERTEX_LOCATION", "us-central1")
        model_name = os.getenv("MODEL_SEARCH", "gemini-2.0-flash-001")
        
        try:
            creds, adc_project = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            logger.info(f"[collector] PROJECT={project} (ADC project={adc_project}) LOCATION={location} MODEL={model_name}")
            logger.info(f"[collector] ADC credentials type={type(creds).__name__}")
        except Exception as e:
            logger.warning(f"[collector] Unable to resolve ADC credentials: {e}")
        
        logger.debug(f"[collector] Initializing genai client for {model_name}")
        
        client = genai.Client(vertexai=True, project=project, location=location)
        
        # Same prompt structure as preview_queries.py - ask for JSON response
        prompt = (
            f'Use Google Search. Return ONLY JSON {{sources:[{{title,url}}]}}. '
            f'Find up to {max_results} results for: {query}'
        )
        
        # Configure with Google Search tool and JSON response
        config = {
            "tools": [{"google_search": {}}],
            "response_mime_type": "application/json",
            "temperature": 0,
            "max_output_tokens": 768,
        }
        
        logger.debug(f"[collector] Sending search request for: {query[:80]}...")
        resp = client.models.generate_content(
            model=model_name,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config=config,
        )
        
        # Inspect raw response for tool execution traces
        try:
            raw_text = getattr(resp, "text", None) or ""
            logger.info(f"[collector] resp.text length={len(raw_text)}")
            cands = getattr(resp, "candidates", []) or []
            logger.info(f"[collector] candidates={len(cands)}")
            for ci, c in enumerate(cands):
                content = getattr(c, "content", None)
                parts = getattr(content, "parts", None) if content else None
                if parts is not None:
                    logger.debug(f"[collector] cand[{ci}] parts sample={str(parts)[:300]}")
                gm = getattr(c, "grounding_metadata", None)
                if gm is not None:
                    logger.debug(f"[collector] cand[{ci}] grounding_metadata={gm}")
        except Exception as e:
            logger.warning(f"[collector] failed to inspect Vertex response: {e}")
        
        # Extract JSON from response
        def extract_first_json_obj(text: str):
            m = re.search(r'\{[\s\S]*\}', text)
            if not m:
                return None
            blk = m.group(0)
            try:
                return json.loads(blk)
            except Exception:
                # Try to recover first balanced JSON object
                depth = 0; start = None
                for i, ch in enumerate(text):
                    if ch == '{':
                        if start is None:
                            start = i
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0 and start is not None:
                            frag = text[start:i+1]
                            try:
                                return json.loads(frag)
                            except Exception:
                                start = None
                return None
        
        text = getattr(resp, "text", None) or str(resp)
        obj = extract_first_json_obj(text) or {}
        sources = obj.get("sources") or []
        
        # Normalize schema
        results = []
        for s in sources:
            title = (s.get("title") or "").strip()
            url = (s.get("url") or "").strip()
            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": "",
                })
        
        logger.info(f"[collector] Vertex search returned {len(results)} results for query")
        return results[:max_results]
        
    except ImportError as e:
        logger.error(f"[collector] Failed to import google.genai: {e}")
        logger.error("[collector] Install with: pip install google-genai")
        return []
    except Exception as e:
        logger.warning(f"[collector] Vertex search failed for query '{query[:50]}...': {e}")
        logger.debug(f"[collector] Full error: {e}", exc_info=True)
        return []


def _execute_mock_search(query: str, company_name: str, domain: Optional[str], max_results: int = 5) -> List[Dict[str, Any]]:
    """
    Mock search for development/testing when Vertex search fails.
    Returns synthetic results based on the query pattern.
    """
    results = []
    base_domain = domain or "example.com"
    
    # Generate mock results based on query type
    if "news" in query.lower() or "press" in query.lower():
        results.append({
            "title": f"{company_name} Announces New Security Features",
            "url": f"https://{base_domain}/news/security-announcement-2024",
            "snippet": f"{company_name} has announced major new security features to enhance protection...",
        })
    
    if "investor" in query.lower() or "earnings" in query.lower():
        results.append({
            "title": f"{company_name} Q3 2024 Earnings Call Transcript",
            "url": f"https://investors.{base_domain}/earnings/q3-2024",
            "snippet": f"{company_name} reported strong Q3 results with revenue growth...",
        })
    
    if "breach" in query.lower() or "incident" in query.lower() or "vulnerability" in query.lower():
        results.append({
            "title": f"{company_name} Security Update",
            "url": f"https://{base_domain}/security/updates/latest",
            "snippet": f"{company_name} has released a security update addressing recent concerns...",
        })
    
    if "acquisition" in query.lower() or "partnership" in query.lower():
        results.append({
            "title": f"{company_name} Strategic Partnership Announcement",
            "url": f"https://{base_domain}/news/partnerships",
            "snippet": f"{company_name} announces strategic partnership to expand market reach...",
        })
    
    if "compliance" in query.lower() or "regulation" in query.lower():
        results.append({
            "title": f"{company_name} Achieves SOC 2 Compliance",
            "url": f"https://{base_domain}/compliance/certifications",
            "snippet": f"{company_name} has achieved SOC 2 Type II compliance certification...",
        })
    
    return results[:max_results]


def collect_web_events_for_company(
    *,
    company_name: str,
    domain: Optional[str] = None,
    max_results: int = 25,
) -> List[Dict]:
    """
    Collector invoked by step1_evidence_grabber.py.
    
    Uses Vertex AI Google Search grounding to collect real web events.
    Falls back to mock data if Vertex search fails.
    """
    # Get query pack from environment or use default
    query_pack_path = os.getenv("WEB_QUERY_PACK", "configs/web_queries.generic.json")
    use_mock = os.getenv("USE_MOCK_SEARCH", "false").lower() == "true"
    
    logger.info(
        "[collector] collect_web_events_for_company(company_name=%r, domain=%r, max_results=%r)",
        company_name, domain, max_results
    )
    
    # Load query pack
    query_pack = _load_query_pack(query_pack_path)
    
    # Build search queries
    queries = _build_queries(query_pack, company_name, domain)
    logger.info(f"[collector] Generated {len(queries)} search queries")
    logger.info(f"[collector] Using query pack: {os.getenv('WEB_QUERY_PACK', 'configs/web_queries.generic.json')}")
    logger.info(f"[collector] use_mock={use_mock} queries={len(queries)}")
    
    if not queries:
        logger.warning("[collector] No valid queries generated, using fallback")
        # Create basic fallback queries
        queries = [
            f'"{company_name}" news',
            f'"{company_name}" security',
            f'"{company_name}" announcement',
        ]
    
    # Execute searches and collect results
    all_results = []
    results_per_query = max(3, max_results // max(len(queries), 1))
    logger.info(f"[collector] results_per_query={results_per_query} (max_results={max_results})")
    
    for i, query in enumerate(queries):
        logger.info(f"[collector] Query {i+1}/{len(queries)}: {query[:120]}...")
        
        if use_mock:
            search_results = _execute_mock_search(query, company_name, domain, max_results=results_per_query)
        else:
            # Try Vertex search first, fall back to mock on failure
            search_results = _execute_vertex_search(query, max_results=results_per_query)
            if not search_results:
                logger.warning(f"[collector] Vertex search returned no results for query {i+1}: {query[:160]}")
                # Don't fall back to mock for every query - just skip
                continue
        
        # Convert to event format
        for result in search_results:
            event = {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "snippet": result.get("snippet", ""),
                "source": "vertex_search" if not use_mock else "mock_search",
                "query": query,
                "query_category": queries[i] if i < len(queries) else "unknown",
                "company": company_name,
                "domain": domain,
                "collected_at": datetime.utcnow().isoformat() + "Z",
            }
            all_results.append(event)
        
        logger.info(f"[collector] Query {i+1} returned {len(search_results)} results")
        
        # Stop if we have enough results
        if len(all_results) >= max_results:
            logger.info(f"[collector] Reached max_results limit ({max_results})")
            break
    
    # Deduplicate by URL
    seen_urls = set()
    unique_results = []
    for event in all_results:
        url = event.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(event)
    
    logger.info(f"[collector] Collected {len(unique_results)} unique events")
    return unique_results[:max_results]