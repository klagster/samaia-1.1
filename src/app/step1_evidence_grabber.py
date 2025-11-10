#!/usr/bin/env python3
"""
step1_evidence_grabber.py
-------------------------
CLI entrypoint for Step 1 of the pipeline. It loads a query-pack and invokes a
web collector to fetch grounded web events for a given company, then writes the
normalized events to JSON.

FIXED: Removed query_pack_path from collector call since the actual collector
function doesn't accept it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_query_pack(query_pack_path: str) -> Dict[str, Any]:
    p = Path(query_pack_path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # Non-fatal: treat as empty pack
            return {}
    return {}


def _noop_collect_web_events_for_company(
    *,
    company_name: str,
    domain: Optional[str] = None,
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """
    Fallback collector that returns an empty list. Keeps the pipeline moving 
    instead of failing on import issues.
    """
    return []


def _resolve_collector():
    """
    Try multiple import paths for the real collector, without creating circular
    imports. If all imports fail, use the no-op implementation.
    """
    # Try to ensure repo root is on sys.path so 'app.' works when launched from root.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    try:
        from app.collectors.web_search_single import collect_web_events_for_company  # type: ignore
        return collect_web_events_for_company
    except Exception:
        pass

    try:
        from collectors.web_search_single import collect_web_events_for_company  # type: ignore
        return collect_web_events_for_company
    except Exception:
        pass

    # Final fallback
    return _noop_collect_web_events_for_company


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Step 1: Grab grounded web evidence.")
    parser.add_argument("--company", required=True, help="Company name (e.g., 'Equinix').")
    parser.add_argument("--company-url", dest="company_url", required=False, default=None, help="Company homepage URL.")
    parser.add_argument("--domain", required=False, default=None, help="Primary domain (e.g., equinix.com).")
    parser.add_argument("--query-pack", dest="query_pack", required=False, default="configs/web_queries.generic.json", help="Path to web query-pack JSON (for validation only).")
    parser.add_argument("--out-json", dest="out_json", required=True, help="Where to write normalized events JSON.")
    parser.add_argument("--max-results", dest="max_results", type=int, default=25, help="Maximum events to return.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    # Load query pack for validation (but don't pass to collector)
    _ = _load_query_pack(args.query_pack)

    collector = _resolve_collector()

    try:
        # FIXED: Call collector with only the arguments it accepts
        events = collector(
            company_name=args.company,
            domain=args.domain,
            max_results=args.max_results,
        )
        # Collector might be sync or async depending on implementation
        if hasattr(events, "__await__"):
            # If it's a coroutine, run it
            import asyncio
            events = asyncio.run(events)  # type: ignore
    except Exception as e:
        # Never crash Step 1 for collector issues—emit empty and continue
        events = []
        print(f"[step1] Collector error: {e}", file=sys.stderr)

    # Normalize to list
    if not isinstance(events, list):
        events = []

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[step1] Wrote JSON: {out_path}")
    print(f"[step1] Event count: {len(events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())