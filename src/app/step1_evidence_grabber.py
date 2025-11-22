#!/usr/bin/env python3
"""
step1_evidence_grabber.py
-------------------------
CLI entrypoint + in-process helper for Step 1 of the pipeline.

It loads a query-pack and invokes the web collector to fetch grounded
web events for a given company, then returns / writes the normalized
list of events.

This version uses the asynchronous collector and handles execution with asyncio.
"""

from __future__ import annotations

import argparse
import json
import sys
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging
import asyncio 

# Set up logging for better output control
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

DEFAULT_QUERY_PACK = "configs/web_queries.combined.json"
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# sys.path bootstrap - CLEANER VERSION
# ---------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
# Try to find the root directory (where 'src' and 'configs' should be)
REPO_ROOT = next((p for p in _THIS_FILE.parents if (p / "src").is_dir() and (p / "configs").is_dir()), None)

if REPO_ROOT and str(REPO_ROOT / "src") not in sys.path:
    # Add the 'src' directory to sys.path
    sys.path.insert(0, str(REPO_ROOT / "src"))
elif str(_THIS_FILE.parent.parent) not in sys.path:
    # Fallback for running directly from the 'app' directory
    sys.path.insert(0, str(_THIS_FILE.parent.parent))

# ---------------------------------------------------------------------------
# Core Helpers
# ---------------------------------------------------------------------------

def _load_query_pack(query_pack_path: str) -> Dict[str, Any]:
    """Lightweight helper to validate that the query-pack JSON is readable."""
    p = Path(query_pack_path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # Non-fatal: treat as empty pack
            logger.warning(f"[_load_query_pack] Found file but failed to parse JSON: {p.name}", exc_info=True)
            return {}
    return {}


def _noop_collect_web_events_for_company(
    *,
    company_name: str,
    domain: Optional[str] = None,
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """Fallback collector that returns an empty list."""
    # NOTE: This must remain a SYNC function if it's the NO-OP fallback.
    return []


# ---------------------------------------------------------------------------
# Import the real collector (now always expects an async function)
# [REMOVED] The global '_collector_callable' assignment is removed for simplicity
# ---------------------------------------------------------------------------

_imported_collector = None
try:
    # Normal case: `from app.collectors.web_search_single import execute_vertex_search_from_pack`
    from app.collectors.web_search_single import (  # type: ignore
        execute_vertex_search_from_pack as _imported_collector_func,
    )
    # Assign the imported async function reference
    _imported_collector = _imported_collector_func
except Exception as e:
    logger.warning(f"[step1] Could not import real collector. Using NO-OP fallback. Error: {e}")

# ---------------------------------------------------------------------------
# In-process callable used by run.py
# ---------------------------------------------------------------------------

async def run_step(
    *,
    company: str,
    company_url: Optional[str] = None,
    domain: Optional[str] = None,
    query_pack: Optional[str] = "configs/web_queries.combined.json",
    max_results: int = 3,
) -> List[Dict[str, Any]]:
    """Execute Step 1 in-process and return a list of event dicts."""

    # Resolve the effective query pack path
    query_pack_path = query_pack if query_pack else DEFAULT_QUERY_PACK

    # Best-effort validation (non-fatal on error)
    _load_query_pack(query_pack_path)

    # Build arguments expected by the collector
    kwargs: Dict[str, Any] = {
        "company": company,
        "domain": domain,
        "pack_path": query_pack_path,
        "max_overall_results": max_results,
    }

    try:
        # [CRITICAL FIX] Directly use the imported function if available, 
        # otherwise use the synchronous NO-OP function. This avoids the 
        # ambiguity of the global callable assignment.
        if _imported_collector:
            # Call the async function and AWAIT it
            events = await _imported_collector(**kwargs)
        else:
            # Call the synchronous fallback function
            events = _noop_collect_web_events_for_company(
                company_name=company,
                domain=domain,
                max_results=kwargs.get("max_overall_results", 10)
            )
            
    except Exception as e:
        logger.exception("[step1] Web search collector failed for company=%s domain=%s", company, domain)
        events = []

    # Normalize
    if not isinstance(events, list):
        logger.warning("[step1] Collector returned non-list events for company=%s; normalizing to empty list", company)
        events = []

    logger.info("[step1] Collected %d web evidence events for company=%s", len(events), company)
    return events


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 1: Grab grounded web evidence.",
    )
    parser.add_argument(
        "--company",
        required=True,
        help="Company name (e.g., 'Equinix').",
    )
    parser.add_argument(
        "--company-url",
        dest="company_url",
        required=False,
        default=None,
        help="Company homepage URL.",
    )
    parser.add_argument(
        "--domain",
        required=False,
        default=None,
        help="Primary domain (e.g., equinix.com).",
    )
    parser.add_argument(
        "--query-pack",
        dest="query_pack",
        required=False,
        default=DEFAULT_QUERY_PACK,
        help="Path to web query-pack JSON.",
    )
    parser.add_argument(
        "--out-json",
        dest="out_json",
        required=True,
        help="Where to write normalized events JSON.",
    )
    parser.add_argument(
        "--max-results",
        dest="max_results",
        type=int,
        default=500,
        help="Maximum events to return.",
    )
    return parser.parse_args()


# [CRITICAL FIX] main() remains synchronous but now uses asyncio.run() to execute the async run_step
async def main() -> int:
    args = _parse_args()

    # The run_step function already validates/loads the query-pack
    kwargs = {
        "company": args.company,
        "company_url": args.company_url,
        "domain": args.domain,
        "query_pack": args.query_pack,
        "max_results": args.max_results,
    }
    
    # [CRITICAL FIX] Use asyncio.run() here to execute the async function run_step 
    # in this synchronous main thread. This starts the loop only if one is not running.
    events = await run_step(**kwargs)

    # Normalize
    if not isinstance(events, list):
        events = []

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Simple completion log
    print(f"[step1] Completed. Wrote {len(events)} events to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    # Ensure logging is configured for the CLI use case
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(main()))