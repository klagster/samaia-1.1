#!/usr/bin/env python3
"""
Session-aware orchestrator for the SAMaiA 5-step pipeline.
FIXED: CLI arguments now match the actual step script interfaces.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

# ADK session/state primitives
from google.adk.sessions import InMemorySessionService, Session
from google.adk.events import Event, EventActions
from google.genai.types import Part, Content

# --------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------

def _print(msg: str) -> None:
    if os.getenv("QUIET", "0") != "1":
        print(msg)

def _load_json(path: str | Path | None, default: Any = None) -> Any:
    if not path:
        return default
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default

def _save_json(path: str | Path, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)

def _normalize_inputs(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                return item
        return {}
    return {}

def _http_url(u: Optional[str]) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

def _sanitize_domain(url: Optional[str]) -> str:
    try:
        from urllib.parse import urlparse
        if not url:
            return ""
        netloc = urlparse(url).netloc
        return netloc or ""
    except Exception:
        return ""

def _coalesce_company_context(inputs: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    Extract company name, URL, and domain from various input formats.
    
    PRIORITY: Target account (the company being sold TO) takes precedence over 
    client (the company running the campaign).
    
    Returns: (company_name, company_url, domain)
    """
    def _first_dict(v: Any) -> Dict[str, Any]:
        if isinstance(v, dict):
            return v
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    return item
        return {}

    ta = _first_dict(inputs.get("target_account"))
    camp = _first_dict(inputs.get("campaign"))
    client = _first_dict(camp.get("clients"))

    # Company name - PRIORITIZE target_account over client
    # The target_account is the company being analyzed (e.g., Equinix)
    # The client is who's running the campaign (e.g., Palo Alto Networks)
    company = (
        ta.get("account_name")       # Supabase target account name (HIGHEST PRIORITY)
        or ta.get("name")             # Alternative field name
        or inputs.get("company_name") # Direct input
        or inputs.get("company")
        or inputs.get("account_name")
        or os.getenv("COMPANY_NAME")
        or client.get("company")      # Fallback to client only if no target
        or client.get("name")
        or "Unknown Company"
    )

    # Company URL - prioritize target_account
    company_url = (
        ta.get("company_website")     # Target account website (HIGHEST PRIORITY)
        or inputs.get("company_url")
        or inputs.get("company_website")
        or client.get("homepage")     # Fallback to client
        or os.getenv("COMPANY_URL")
        or ""
    )

    # Domain - explicit first, then derive from URL
    domain = inputs.get("domain")
    if not domain:
        url = company_url or ""
        if url.startswith("http://") or url.startswith("https://"):
            try:
                from urllib.parse import urlparse
                host = urlparse(url).netloc.lower()
                # Strip www. prefix
                if host.startswith("www."):
                    host = host[4:]
                if host and host != "example.com":
                    domain = host
            except Exception:
                domain = ""
        else:
            domain = ""

    # Clean up example.com noise
    if company_url and "example.com" in company_url:
        company_url = ""
    if domain == "example.com":
        domain = ""

    return company, company_url, domain

def _derive_session_id(inputs: Dict[str, Any], fallback_company: str) -> str:
    cid = inputs.get("campaign_id")
    tid = inputs.get("target_account_id")
    if cid and tid:
        return f"{cid}_{tid}"
    base = (inputs.get("company_name") or fallback_company or "session").replace(" ", "_")
    return f"{base}_session"

def _run(cmd: List[str], cwd: Optional[str] = None, extra_env: Optional[Dict[str, str]] = None) -> int:
    """Execute a command with proper error handling."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    
    # Validate command arguments - no None values allowed
    clean_cmd = []
    for arg in cmd:
        if arg is None:
            _print(f"[ERROR] Command contains None value: {cmd}")
            raise ValueError(f"Command argument cannot be None: {cmd}")
        clean_cmd.append(str(arg))
    
    _print(f"[orchestrator] exec: {' '.join(shlex.quote(c) for c in clean_cmd)}")
    proc = subprocess.Popen(clean_cmd, cwd=cwd, env=env)
    return proc.wait()

async def _append_event(session_service: InMemorySessionService, session: Session, name: str, text: str, delta: Dict[str, Any]) -> None:
    parts = [Part.from_text(text=text)]
    ev = Event(
        author="app",
        content=Content(role="agent", parts=parts),
        actions=EventActions(state_delta={**delta, "_event_name": name}),
    )
    await session_service.append_event(session, ev)

# --------------------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="SAMaiA session-aware orchestrator")
    parser.add_argument("--inputs", default=None, help="Path to user inputs JSON")
    parser.add_argument("--mapping-insights", default=None, help="Path to mapping_insights JSON")
    parser.add_argument("--signal-categories", default=None, help="Path to signal_categories JSON")
    parser.add_argument("--campaign-challenges", default=None, help="Path to campaign/customer challenges JSON")
    parser.add_argument("--out-dir", default=os.getenv("OUT_DIR", ".outputs"), help="Directory for step outputs")
    parser.add_argument("--quiet", action="store_true", help="Suppress most console output")
    args = parser.parse_args()

    if args.quiet:
        os.environ["QUIET"] = "1"

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs_raw = _load_json(args.inputs, default={}) or {}
    inputs = _normalize_inputs(inputs_raw)

    company, company_url, domain = _coalesce_company_context(inputs)
    if not isinstance(inputs, dict):
        inputs = {}
    if not domain and company_url:
        domain = _sanitize_domain(company_url)
    
    # Validate we have a company name
    if not company or company == "Unknown Company":
        _print("[orchestrator] WARNING: No company name found in inputs!")
        _print(f"[orchestrator] Available input keys: {list(inputs.keys())}")
        if "target_account" in inputs:
            _print(f"[orchestrator] target_account keys: {list(inputs['target_account'].keys()) if isinstance(inputs['target_account'], dict) else 'not a dict'}")
        if "campaign" in inputs:
            camp = inputs["campaign"]
            _print(f"[orchestrator] campaign keys: {list(camp.keys()) if isinstance(camp, dict) else 'not a dict'}")
            if isinstance(camp, dict) and "clients" in camp:
                _print(f"[orchestrator] campaign.clients: {camp['clients']}")
    
    _print(f"[orchestrator] Resolved: company={company!r}, url={company_url!r}, domain={domain!r}")

    # ADK session
    app_name = os.getenv("ADK_APP_NAME", "samaia")
    user_id = os.getenv("ADK_USER_ID", "system")
    session_id = os.getenv("ADK_SESSION_ID", _derive_session_id(inputs, company))

    session_service = InMemorySessionService()
    session: Session = await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        state={
            "company_name": company,
            "company_url": company_url,
            "domain": domain,
            "time_window": inputs.get("time_window", os.getenv("TIME_WINDOW", "last 12–18 months")),
        },
    )
    await _append_event(session_service, session, "session_started", f"Session started for {company}", {"status": "started"})

    child_env = {
        "ADK_APP_NAME": app_name,
        "ADK_USER_ID": user_id,
        "ADK_SESSION_ID": session_id,
    }

    py = sys.executable or "python"
    query_pack = os.getenv("WEB_QUERY_PACK", "configs/web_queries.generic.json")

    # ---- Step 1: Web Evidence Grabber
    # FIXED: Removed --query-pack argument, collector doesn't accept it
    step1_json = out_dir / "step1_web_evidence.json"
    if Path("src/app/step1_evidence_grabber.py").exists():
        rc = _run(
            [
                py, "src/app/step1_evidence_grabber.py",
                "--company", company,
                "--company-url", company_url,
                "--domain", domain,
                "--out-json", str(step1_json),
                "--max-results", os.getenv("WEB_MAX_RESULTS", "25"),
            ],
            extra_env=child_env,
        )
        if rc != 0:
            _print(f"[warn] Step 1 exited with {rc}")
        web_evidence = _load_json(step1_json, default=[])
        await _append_event(
            session_service, session, "step1_done",
            "Collected grounded web evidence.",
            {"step1_count": len(web_evidence) if isinstance(web_evidence, list) else 0},
        )
    else:
        _print("[orchestrator] Step 1 script not found; skipping.")
        web_evidence = []

    # ---- Step 2: Evidence Harvester
    # FIXED: Changed --inputs/--web-evidence to --company/--raw-events
    step2_json = out_dir / "step2_problems.json"
    step2_md   = out_dir / "step2_problems.md"
    if Path("src/app/step2_evidence_harvester.py").exists():
        rc = _run(
            [
                py, "src/app/step2_evidence_harvester.py",
                "--company", company,
                "--raw-events", str(step1_json),
                "--out-json", str(step2_json),
                "--session-dir", str(out_dir),
            ],
            extra_env=child_env,
        )
        if rc != 0:
            _print(f"[warn] Step 2 exited with {rc}")
        problems = _load_json(step2_json, default={})
        await _append_event(
            session_service, session, "step2_done",
            "Generated evidenced problems.",
            {"step2_problem_count": len(problems.get("problems", [])) if isinstance(problems, dict) else 0},
        )
    else:
        _print("[orchestrator] Step 2 script not found; skipping.")
        problems = {}

    # ---- Step 3: Hypotheses Generator
    # FIXED: Changed --issues/--mapping-insights to --evidence/--company
    step3_json = out_dir / "step3_hypotheses.json"
    step3_md   = out_dir / "step3_hypotheses.md"
    if Path("src/app/step3_hypotheses_generator.py").exists():
        rc = _run(
            [
                py, "src/app/step3_hypotheses_generator.py",
                "--evidence", str(step2_json),
                "--company", company,
                "--time-window", inputs.get("time_window", "last 12-18 months"),
                "--out-json", str(step3_json),
                "--out-md", str(step3_md),
                "--session-dir", str(out_dir),
            ],
            extra_env=child_env,
        )
        if rc != 0:
            _print(f"[warn] Step 3 exited with {rc}")
        hypotheses = _load_json(step3_json, default={})
        await _append_event(
            session_service, session, "step3_done",
            "Generated hypotheses seed.",
            {"step3_issue_count": len(hypotheses.get("issues", [])) if isinstance(hypotheses, dict) else 0},
        )
    else:
        _print("[orchestrator] Step 3 script not found; skipping.")
        hypotheses = {}

    # ---- Step 4: Alignment
    # FIXED: Changed --campaign-challenges to --taxonomy, added --company
    step4_json = out_dir / "step4_alignments.json"
    step4_md   = out_dir / "step4_alignments.md"
    
    # Load taxonomy from campaign-challenges if provided
    taxonomy_path = None
    if args.campaign_challenges:
        taxonomy_path = args.campaign_challenges
    
    if Path("src/app/step4_alignment.py").exists():
        cmd = [
            py, "src/app/step4_alignment.py",
            "--issues", str(step3_json),
            "--company", company,
            "--time-window", inputs.get("time_window", "last 12-18 months"),
            "--out-json", str(step4_json),
            "--out-md", str(step4_md),
            "--session-dir", str(out_dir),
        ]
        if taxonomy_path:
            cmd.extend(["--taxonomy", taxonomy_path])
        
        rc = _run(cmd, extra_env=child_env)
        if rc != 0:
            _print(f"[warn] Step 4 exited with {rc}")
        alignments = _load_json(step4_json, default={})
        await _append_event(
            session_service, session, "step4_done",
            "Aligned issues to campaign challenges.",
            {"step4_alignment_count": len(alignments.get("alignments", [])) if isinstance(alignments, dict) else 0},
        )
    else:
        _print("[orchestrator] Step 4 script not found; skipping.")
        alignments = {}

    # ---- Step 5: Compelling Events
    # This one looks correct already
    step5_json = out_dir / "step5_compelling_events.json"
    step5_md   = out_dir / "step5_compelling_events.md"
    if Path("src/app/step5_compelling_events.py").exists():
        rc = _run(
            [
                py, "src/app/step5_compelling_events.py",
                "--issues", str(step2_json),
                "--alignments", str(step4_json),
                "--evidence", str(step1_json),
                "--company", company,
                "--out-json", str(step5_json),
                "--out-md", str(step5_md),
                "--strict", os.getenv("STEP5_STRICT", "medium"),
            ],
            extra_env=child_env,
        )
        if rc != 0:
            _print(f"[warn] Step 5 exited with {rc}")
        events = _load_json(step5_json, default={})
        await _append_event(
            session_service, session, "step5_done",
            "Generated compelling events.",
            {"step5_event_count": len(events.get("compelling_events", [])) if isinstance(events, dict) else 0},
        )
    else:
        _print("[orchestrator] Step 5 script not found; skipping.")
        events = {}

    # ---- Final summary
    summary = {
        "step1_json": str(step1_json) if step1_json.exists() else None,
        "step2_json": str(step2_json) if step2_json.exists() else None,
        "step3_json": str(step3_json) if step3_json.exists() else None,
        "step4_json": str(step4_json) if step4_json.exists() else None,
        "step5_json": str(step5_json) if step5_json.exists() else None,
    }
    _print("[orchestrator] Finished. Outputs:")
    for k, v in summary.items():
        _print(f"  {k}: {v or '(skipped)'}")

    # Emit tail-friendly lines
    try:
        step2_obj = _load_json(step2_json, default={}) or {}
        step3_obj = _load_json(step3_json, default={}) or {}
        step4_obj = _load_json(step4_json, default={}) or {}
        print("__STEP2_JSON__:" + json.dumps(step2_obj, separators=(",", ":")))
        print("__STEP3_JSON__:" + json.dumps(step3_obj, separators=(",", ":")))
        print("__STEP4_JSON__:" + json.dumps(step4_obj, separators=(",", ":")))
        print("__PIPELINE_JSON__:" + json.dumps({
            "step2_json": step2_obj,
            "step3_json": step3_obj,
            "step4_json": step4_obj
        }, separators=(",", ":")))
    except Exception:
        pass

    await _append_event(session_service, session, "pipeline_complete", "Pipeline complete.", {"status": "complete"})

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())