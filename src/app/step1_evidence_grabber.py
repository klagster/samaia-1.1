#!/usr/bin/env python3
"""
step1_evidence_grabber.py
-------------------------
CLI entrypoint + in-process helper for Step 1 of the pipeline.

It loads a query-pack and invokes the web collector to fetch grounded
web events for a given company, then returns / writes the normalized
list of events.

This version is designed to work in both contexts:
  * As a standalone script, e.g.:
      python src/app/step1_evidence_grabber.py \
        --company "RingCentral" \
        --company-url "https://www.ringcentral.com" \
        --query-pack configs/web_queries.generic.json \
        --out-json .outputs/step1_test.json

  * As an in-process module imported by run.py, where run.py calls
      from app.step1_evidence_grabber import run_step
      events = run_step(...)

It uses the existing collector at:
    src/app/collectors/web_search_single.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# sys.path bootstrap so imports work both locally and in GCF
# ---------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()

# Walk upwards and, if we find the typical repo layout, add `src` to sys.path
# so that `import app.*` works regardless of how this file is invoked.
for candidate in (_THIS_FILE.parents[3], _THIS_FILE.parents[2], _THIS_FILE.parents[1], Path.cwd()):
    try:
        src_dir = candidate / "src"
        if (src_dir / "app" / "collectors" / "web_search_single.py").exists():
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))
    except Exception:
        # Best-effort only; never crash on path probing
        pass


def _load_query_pack(query_pack_path: str) -> Dict[str, Any]:
    """Lightweight helper to validate that the query-pack JSON is readable.

    The collector itself takes the path and is responsible for using it.
    Here we just try to load it so obvious JSON / path errors surface early.
    """
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
    # query_pack_path: Optional[str] = None,  # not used in the no-op
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """Fallback collector that returns an empty list.

    Keeps the pipeline moving instead of failing on import issues.
    """
    return []


# ---------------------------------------------------------------------------
# Import the real collector with robust fallbacks
# ---------------------------------------------------------------------------

try:
    # Normal case when `src` is on sys.path and `app` is the top-level package
    from app.collectors.web_search_single import (  # type: ignore
        collect_web_events_for_company as _collector,
    )
    print(
        "[step1] Using collector: app.collectors.web_search_single.collect_web_events_for_company",
        file=sys.stderr,
    )
except Exception:
    try:
        # Fallback if this file is executed directly out of src/app so that
        # `collectors` is the top-level package.
        from collectors.web_search_single import (  # type: ignore
            collect_web_events_for_company as _collector,
        )
        print(
            "[step1] Using collector: collectors.web_search_single.collect_web_events_for_company",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"[step1] ERROR: could not import web_search_single collector (falling back to no-op): {e!r}",
            file=sys.stderr,
        )
        _collector = _noop_collect_web_events_for_company  # type: ignore


# ---------------------------------------------------------------------------
# In-process callable used by run.py to avoid subprocess + disk I/O
# ---------------------------------------------------------------------------

def run_step(
    *,
    company: str,
    company_url: Optional[str] = None,
    domain: Optional[str] = None,
    query_pack: Optional[str] = "configs/web_queries.generic.json",
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """Execute Step 1 in-process and return a list of event dicts.

    Parameters
    ----------
    company: str
        Company name (e.g., "Equinix").
    company_url: Optional[str]
        Company homepage URL (currently not used by the collector, kept for interface symmetry).
    domain: Optional[str]
        Primary domain, e.g., "equinix.com".
    query_pack: Optional[str]
        Path to the web query-pack JSON. Currently *not* passed through to the
        collector to maintain compatibility with existing signatures; the
        collector uses its own default query-pack.
    max_results: int
        Maximum number of events to request from the collector.
    """

    # Light validation of the query-pack file (non-fatal on errors). This just
    # surfaces obvious JSON / path problems early when running via CLI.
    if query_pack:
        _load_query_pack(query_pack)

    kwargs: Dict[str, Any] = {
        "company_name": company,
        "domain": domain,
        "max_results": max_results,
    }
    # IMPORTANT:
    # We NO LONGER pass `query_pack_path` (or any query-pack kwarg) into the
    # collector, because the current collector signature does not accept it:
    #
    #   collect_web_events_for_company() got an unexpected keyword argument 'query_pack_path'
    #
    # This restores the pre-refactor behavior where the collector uses its
    # internal default query-pack.

    try:
        events = _collector(**kwargs)  # type: ignore[arg-type]

        # Support async collectors transparently
        if hasattr(events, "__await__"):
            import asyncio

            events = asyncio.run(events)  # type: ignore[assignment]
    except Exception as e:  # pragma: no cover - defensive
        # Never crash the pipeline; return empty list and let the caller decide
        print(f"[step1] Collector error (in-process): {e}", file=sys.stderr)
        events = []

    # Normalize
    if not isinstance(events, list):
        events = []
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
        default="configs/web_queries.generic.json",
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
        default=25,
        help="Maximum events to return.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    # Light validation of the query-pack file (non-fatal on errors)
    _ = _load_query_pack(args.query_pack)

    events = run_step(
        company=args.company,
        company_url=args.company_url,
        domain=args.domain,
        query_pack=args.query_pack,
        max_results=args.max_results,
    )

    # Normalize to list
    if not isinstance(events, list):
        events = []

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[step1] Wrote JSON: {out_path}")
    print(f"[step1] Event count: {len(events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())