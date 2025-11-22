import os
import json
from datetime import datetime, timezone
from typing import List, Dict, Any
import logging
import asyncio 
import re 
import time
from google import genai
from google.genai.types import HttpOptions
from google.genai.errors import APIError 
from google.auth import default as google_auth_default

# NEW IMPORT: Add tenacity for robust error handling
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type 

logger = logging.getLogger(__name__)

# Shared log context helper
def _log_ctx() -> str:
    """Return a stable context string for logs (run_id, campaign_id, target_account_id)."""
    run_id = os.getenv("RUN_ID")
    campaign_id = os.getenv("CAMPAIGN_ID")
    ta_id = os.getenv("TARGET_ACCOUNT_ID")
    return f"[ctx run_id={run_id} campaign_id={campaign_id} ta_id={ta_id}]"

# ---------------------------
# Rate Limiting & Model Configuration (Step 1)
# ---------------------------
# These defaults can be overridden from the campaign record via env vars
# (set in src/app/run.py using TA_STEP1_* keys).
try:
    PROVISIONED_QPM = int(os.getenv("TA_STEP1_EXTERNAL_PROVISIONED_QPM", "60"))
    logger.debug(f"[collector]{_log_ctx()} Loaded PROVISIONED_QPM from env: {PROVISIONED_QPM}")
except ValueError:
    logger.debug(f"[collector]{_log_ctx()} *** Invalid PROVISIONED_QPM in env; using default 60 ***")
    PROVISIONED_QPM = 60

try:
    SAFETY_MARGIN = float(os.getenv("TA_STEP1_EXTERNAL_SAFETY_MARGIN", "0.75"))
    logger.debug(f"[collector]{_log_ctx()} Loaded SAFETY_MARGIN from env: {SAFETY_MARGIN}")
except ValueError:
    SAFETY_MARGIN = 0.75

# Clamp to reasonable ranges consistent with DB constraints
if PROVISIONED_QPM < 1:
    PROVISIONED_QPM = 1
if PROVISIONED_QPM > 1000:
    PROVISIONED_QPM = 1000

if SAFETY_MARGIN < 0.1:
    SAFETY_MARGIN = 0.1
if SAFETY_MARGIN > 1.0:
    SAFETY_MARGIN = 1.0

EFFECTIVE_QPM = int(PROVISIONED_QPM * SAFETY_MARGIN)

try:
    CONCURRENCY_LIMIT = int(os.getenv("TA_STEP1_EXTERNAL_CONCURRENCY", "3"))
    logger.debug(f"[collector]{_log_ctx()} Loaded CONCURRENCY_LIMIT from env: {CONCURRENCY_LIMIT}")
except ValueError:
    CONCURRENCY_LIMIT = 3

if CONCURRENCY_LIMIT < 1:
    CONCURRENCY_LIMIT = 1
if CONCURRENCY_LIMIT > 50:
    CONCURRENCY_LIMIT = 50

# Temperature and max_output_tokens for the search model
try:
    STEP1_TEMPERATURE = float(os.getenv("TA_STEP1_TEMPERATURE", "0.2"))
    logger.debug(f"[collector]{_log_ctx()} Loaded STEP1_TEMPERATURE from env: {STEP1_TEMPERATURE}")
except ValueError:
    STEP1_TEMPERATURE = 0.2

try:
    STEP1_MAX_OUTPUT_TOKENS = int(os.getenv("TA_STEP1_MAX_OUTPUT_TOKENS", "1024"))
    logger.debug(f"[collector]{_log_ctx()} Loaded STEP1_MAX_OUTPUT_TOKENS from env: {STEP1_MAX_OUTPUT_TOKENS}")
except ValueError:
    STEP1_MAX_OUTPUT_TOKENS = 1024

logger.info(
    f"[collector]{_log_ctx()} Configured for {PROVISIONED_QPM} QPM "
    f"(effective {EFFECTIVE_QPM} with margin {SAFETY_MARGIN}), "
    f"concurrency={CONCURRENCY_LIMIT}, temp={STEP1_TEMPERATURE}, "
    f"max_tokens={STEP1_MAX_OUTPUT_TOKENS}"
)

# ---------------------------
# Token Bucket Rate Limiter
# ---------------------------
class TokenBucketRateLimiter:
    """Token bucket rate limiter for smooth request distribution."""
    
    def __init__(self, requests_per_minute: int):
        self.requests_per_minute = requests_per_minute
        self.tokens = float(requests_per_minute)
        self.max_tokens = float(requests_per_minute)
        self.last_update = time.time()
        self.lock = asyncio.Lock()
        
        # Calculate refill rate (tokens per second)
        self.refill_rate = requests_per_minute / 60.0
        
        logger.info(f"[RateLimiter] Initialized: {requests_per_minute} QPM, {self.refill_rate:.2f} tokens/sec")
    
    async def acquire(self):
        """Acquire a token, waiting if necessary."""
        async with self.lock:
            now = time.time()
            
            # Refill tokens based on time elapsed
            elapsed = now - self.last_update
            self.tokens = min(self.max_tokens, self.tokens + (elapsed * self.refill_rate))
            self.last_update = now
            
            # If we don't have a full token, wait until we do
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.refill_rate
                logger.debug(f"[RateLimiter] Waiting {wait_time:.2f}s for token (current: {self.tokens:.2f})")
                await asyncio.sleep(wait_time)
                
                # Update after sleep
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(self.max_tokens, self.tokens + (elapsed * self.refill_rate))
                self.last_update = now
            
            # Consume one token
            self.tokens -= 1.0
            logger.debug(f"[RateLimiter] Token acquired. Remaining: {self.tokens:.2f}/{self.max_tokens}")

# Global rate limiter instance
_rate_limiter = TokenBucketRateLimiter(requests_per_minute=EFFECTIVE_QPM)

# ---------------------------
# Shared Vertex genai client
# ---------------------------

_client = None

def _get_genai_client():
    """Return a shared google.genai client configured for Vertex AI (Synchronous)."""
    global _client
    if _client is not None:
        return _client

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("VERTEX_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    model_name = os.getenv("MODEL_SEARCH", "gemini-2.5-pro") 

    try:
        creds, adc_project = google_auth_default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        logger.debug(
            f"[collector] _get_genai_client ADC project={adc_project}, "
            f"creds_type={type(creds).__name__}, MODEL={model_name}, "
            f"PROJECT={project}, LOCATION={location}"
        )
    except Exception as e:
        logger.warning(f"[collector] _get_genai_client unable to resolve ADC credentials: {e}")

    _client = genai.Client(
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
    return _client

def _get_async_genai_client():
    """Return the shared asynchronous google.genai client."""
    client = _get_genai_client()
    return client.aio


# Helper to load a query pack JSON file
def _load_query_pack(path: str) -> Dict[str, Any]:
    """Load a query pack JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            pack = json.load(f)
            logger.debug(f"[collector] Loaded query pack from {path} with {len(pack.get('categories', []))} categories")
            return pack
    except Exception as e:
        logger.warning(f"[collector] Failed to load query pack '{path}': {e}")
        return {}


# Helper to extract JSON from response text
def _extract_first_json_obj(text: str):
    """Safely extracts the first valid JSON object from a string."""
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    
    blk = m.group(0)
    try:
        return json.loads(blk)
    except Exception:
        # Fallback: Try to recover first balanced JSON object
        depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == "{":
                if start is None:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    frag = text[start : i + 1]
                    try:
                        return json.loads(frag)
                    except Exception:
                        start = None
        return None


# Retry decorator with faster backoff (since we have rate limiting)
@retry(
    wait=wait_exponential(min=2, max=30),  # Faster backoff: 2s to 30s
    stop=stop_after_attempt(3),             # Only 3 retries (rate limiter prevents most 429s)
    retry=retry_if_exception_type(APIError),
    reraise=True
)
async def _execute_vertex_search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Execute web search using Vertex AI's Google Search via the google.genai SDK asynchronously."""
    
    logger.debug(f"[collector] BEGIN vertex search: query='{query[:60]}...', max_results={max_results}")
    
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("VERTEX_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    model_name = os.getenv("MODEL_SEARCH", "gemini-2.5-pro")

    # CRITICAL: Acquire rate limit token before making request
    await _rate_limiter.acquire()

    client_aio = _get_async_genai_client()

    # Prompt: force *only* JSON, grounded in Google Search citations
    prompt = (
        "You are a research assistant using Google Search as your only data source.\n"
        "You must NOT hallucinate URLs or articles.\n"
        "Use the Google Search tool to find real results, then return ONLY strict JSON.\n\n"
        "Return JSON with this exact structure (no extra fields, no comments, no explanations):\n"
        "{\n"
        "  \"sources\": [\n"
        "    {\n"
        "      \"title\": \"string\",\n"
        "      \"url\": \"string\",\n"
        "      \"snippet\": \"short description if available\",\n"
        "      \"published_at\": \"ISO 8601 date-time if you can find it, else null\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"Rules:\n"
        f"- If you cannot find any relevant results, return {{\"sources\": []}}.\n"
        f"- Do NOT wrap the JSON in markdown fences.\n"
        f"- Do NOT include any trailing text after the closing }}.\n\n"
        f"Now perform a grounded search for: {query}\n"
        f"Return up to {max_results} high-quality, recent results that mention this company or topic."
    )

    config = {
        "tools": [{"google_search": {}}],
        "response_mime_type": "application/json",
        "temperature": STEP1_TEMPERATURE,
        "max_output_tokens": STEP1_MAX_OUTPUT_TOKENS,
    }

    logger.debug(f"[collector] Sending grounded search request for: {query[:80]}...")

    try:
        resp = await client_aio.models.generate_content(
            model=model_name,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config=config,
        )

        # Extract results
        text = getattr(resp, "text", None) or str(resp)
        obj = _extract_first_json_obj(text) or {}
        sources = obj.get("sources") or []

        results = []
        for s in sources:
            title = (s.get("title") or "").strip()
            url = (s.get("url") or "").strip()
            snippet = (s.get("snippet") or "").strip()
            raw_date = (s.get("published_at") or s.get("date") or "").strip()
            source_date_iso = None
            if raw_date:
                try:
                    dt = (
                        datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        if "T" in raw_date or "+" in raw_date or "Z" in raw_date
                        else datetime.fromisoformat(raw_date)
                    )
                    source_date_iso = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                except Exception:
                    source_date_iso = raw_date

            if title and url:
                results.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": snippet,
                        "source_date_iso": source_date_iso,
                    }
                )

        logger.debug(
            f"[collector] END vertex search: found {len(results)} results for query='{query[:60]}...'"
        )
        return results[:max_results]
        
    except ImportError as e:
        logger.error(f"[collector] Failed to import google.genai dependencies: {e}")
        raise
    except Exception as e:
        logger.error(f"[collector] Search failed for query '{query[:50]}...': {e}")
        logger.debug(f"[collector] Full error: {e}", exc_info=True)
        raise e


async def execute_vertex_search_from_pack(
    company: str,
    domain: str,
    pack_path: str | None = None,
    max_results_per_query: int = 2,
    max_overall_results: int = 10,
) -> List[Dict[str, Any]]:
    """Execute grounded web search using a query pack with optimized concurrency."""

    pack_path = pack_path or "configs/web_queries.combined.json"

    pack = _load_query_pack(pack_path)
    categories = pack.get("categories") or []

    logger.info(
        f"[collector] Running query pack for company='{company}', domain='{domain}', "
        f"categories={len(categories)}, pack='{pack_path}'"
    )

    # Use optimized concurrency with semaphore
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    logger.info(
        f"[collector] Rate limiting: {EFFECTIVE_QPM} QPM with {CONCURRENCY_LIMIT} concurrent tasks"
    )

    # Wrapper function with semaphore
    async def throttled_search(query, max_results):
        async with semaphore:
            return await _execute_vertex_search(query, max_results)

    # Prepare all tasks
    tasks = []
    task_meta = {} 

    for category in categories:
        cat_name = (category.get("name") or "Uncategorized").strip() or "Uncategorized"
        for tmpl in category.get("queries") or []:
            if not tmpl:
                continue

            rendered = (
                tmpl.replace("{{company}}", company or "")
                .replace("{{domain}}", domain or "")
            )
            
            coro = throttled_search(rendered, max_results=max_results_per_query) 
            tasks.append(coro)
            task_meta[coro] = {"category": cat_name, "query": rendered}

    num_tasks = len(tasks)
    logger.info(f"[collector] Prepared {num_tasks} search tasks.")
    
    # Estimate time
    estimated_time_seconds = (num_tasks / EFFECTIVE_QPM) * 60
    logger.info(
        f"[collector] Estimated completion time: {estimated_time_seconds/60:.1f} minutes "
        f"({num_tasks} queries at {EFFECTIVE_QPM} QPM)"
    )
    
    # Execute all tasks concurrently
    start_time = time.time()
    results_list = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed_time = time.time() - start_time

    # Process results
    results: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    success_count = 0
    error_count = 0

    for task_result, coro in zip(results_list, tasks):
        meta = task_meta.get(coro, {"category": "N/A", "query": "N/A"})
        cat_name = meta["category"]
        rendered_query = meta["query"]
        
        if isinstance(task_result, Exception):
            error_count += 1
            logger.warning(
                f"[collector] Query failed for category='{cat_name}' query='{rendered_query[:60]}...': "
                f"{type(task_result).__name__}"
            )
            continue
        
        success_count += 1
        hits = task_result

        for h in hits:
            url = (h.get("url") or "").strip()
            if not url or url in seen_urls:
                continue

            seen_urls.add(url)
            enriched = dict(h)
            enriched["category"] = cat_name
            enriched["query"] = rendered_query
            results.append(enriched)

            if len(results) >= max_overall_results:
                logger.info(
                    f"[collector] Reached max_overall_results={max_overall_results}; "
                    "stopping processing."
                )
                break
        
        if len(results) >= max_overall_results:
            break

    actual_qpm = (success_count / elapsed_time) * 60 if elapsed_time > 0 else 0
    logger.info(
        f"[collector] Query pack completed in {elapsed_time:.1f}s: "
        f"{success_count} succeeded, {error_count} failed, "
        f"actual rate: {actual_qpm:.1f} QPM"
    )
    logger.info(f"[collector] Produced {len(results)} unique results")
    
    return results