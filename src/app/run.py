#!/usr/bin/env python3
"""
Session-aware orchestrator for the SAMaiA 5-step pipeline.
Now also persists all 5 step outputs to Supabase.target_accounts for the
current target_account_id detected from inputs.

Env required (on GCF/locally):
- SUPABASE_URL
- SUPABASE_SERVICE_KEY
Optional:
- QUIET=1 to reduce console
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
import hmac, hashlib, uuid
import logging
import asyncio # [NEW] Added for async operations

# Ensure INFO-level logs from redirected stdout are emitted
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    force=True,
)

# Artifact controls
KEEP_JSON = os.getenv("KEEP_JSON", "1") != "0"      # default keep JSON
KEEP_MD = os.getenv("KEEP_MD", "0") == "1"           # default do NOT write MD
CLEAN_OUTPUTS = os.getenv("CLEAN_OUTPUTS", "0") == "1"  # delete artifacts after run

# Prefer in-process step execution; fall back to subprocess + files if unavailable
try:
    from src.app import step1_evidence_grabber as step1
    HAS_STEP1 = hasattr(step1, "run_step")
except Exception:
    step1, HAS_STEP1 = None, False

try:
    from src.app import step2_evidence_harvester as step2
    HAS_STEP2 = hasattr(step2, "run_step")
except Exception:
    step2, HAS_STEP2 = None, False

try:
    from src.app import step3_hypotheses_generator as step3
    HAS_STEP3 = hasattr(step3, "run_step")
except Exception:
    step3, HAS_STEP3 = None, False

try:
    from src.app import step4_alignment as step4
    HAS_STEP4 = hasattr(step4, "run_step")
except Exception:
    step4, HAS_STEP4 = None, False

try:
    from src.app import step5_compelling_events as step5
    HAS_STEP5 = hasattr(step5, "run_step")
except Exception:
    step5, HAS_STEP5 = None, False

# ADK session/state primitives
from google.adk.sessions import InMemorySessionService, Session
from google.adk.events import Event, EventActions
from google.genai.types import Part, Content

# --------------------------------------------------------------------------------------
# Supabase writer (simple PostgREST PATCH)
# --------------------------------------------------------------------------------------

import urllib.request
import urllib.error

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
# Guard against accidentally including /rest/v1 in the env var
if SUPABASE_URL.endswith("/rest/v1"):
    SUPABASE_URL = SUPABASE_URL[: -len("/rest/v1")]
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _sb_patch_target_account(ta_id: Optional[str], patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    PATCH /rest/v1/target_accounts?id=eq.<uuid>
    """
    if not ta_id:
        return {"ok": False, "error": "missing target_account_id"}
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {"ok": False, "error": "missing SUPABASE_URL or SUPABASE_SERVICE_KEY"}

    url = f"{SUPABASE_URL}/rest/v1/target_accounts?id=eq.{ta_id}"
    body = json.dumps(patch).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"ok": 200 <= resp.getcode() < 300, "status": resp.getcode()}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        return {"ok": False, "status": e.code, "error": detail[:500]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _save_step_json(ta_id: Optional[str], column: str, payload: Any) -> None:
    """
    Persist a step's JSON payload to a specific column and bump analysis_updated_at.
    Best-effort: prints a warning if it fails but does not crash the pipeline.
    """
    patch = {
        column: payload,
        "analysis_updated_at": datetime.utcnow().isoformat() + "Z",
    }
    res = _sb_patch_target_account(ta_id, patch)
    print(
        f"[save->supabase] ta_id={ta_id} column={column} ok={res.get('ok')} status={res.get('status')} error={res.get('error')}",
        flush=True,
    )
    if not res.get("ok"):
        print(f"[save->supabase] WARN: failed to write {column}: {res}", flush=True)

# --------------------------------------------------------------------------------------
# Webhook helpers (HMAC-SHA256 signed JSON POST)
# --------------------------------------------------------------------------------------

def _hmac_sha256_hex(secret: str, body: bytes) -> str:
    try:
        return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    except Exception:
        return ""


def _post_webhook(url: str, secret: str, event_type: str, payload: dict) -> dict:
    # Require run_id to be present; never silently generate a new one
    payload = dict(payload or {})
    run_id = payload.get("run_id") or os.environ.get("RUN_ID")
    if not run_id:
        return {"ok": False, "error": "missing run_id for webhook"}

    # Ensure the JSON body also carries the run_id
    payload["run_id"] = run_id
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "X-Event-Type": event_type,
        "X-Run-Id": run_id,
        "X-Delivery-Id": str(uuid.uuid4()),
    }
    if secret:
        sig = _hmac_sha256_hex(secret, body)
        if sig:
            headers["X-Signature"] = f"sha256={sig}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            return {"ok": 200 <= status < 300, "status": status}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        return {"ok": False, "status": e.code, "error": detail[:500]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

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

def _first_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        for item in v:
            if isinstance(item, dict):
                return item
    return {}

def _coalesce_company_context(inputs: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    Extract company name, URL, and domain from various input formats.
    PRIORITY: target_account (the company being analyzed) over client/campaign.
    """
    ta = _first_dict(inputs.get("target_account"))
    camp = _first_dict(inputs.get("campaign"))
    client = _first_dict(camp.get("clients"))

    company = (
        ta.get("account_name")
        or ta.get("name")
        or inputs.get("company_name")
        or inputs.get("company")
        or inputs.get("account_name")
        or os.getenv("COMPANY_NAME")
        or client.get("company")
        or client.get("name")
        or "Unknown Company"
    )
    company_url = (
        ta.get("company_website")
        or inputs.get("company_url")
        or inputs.get("company_website")
        or client.get("homepage")
        or os.getenv("COMPANY_URL")
        or ""
    )
    domain = inputs.get("domain")
    if not domain and company_url:
        try:
            from urllib.parse import urlparse
            host = urlparse(company_url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host and host != "example.com":
                domain = host
        except Exception:
            domain = ""
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

def _extract_ids_from_inputs(inputs: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Attempt to find campaign_id and target_account_id from the inputs blob.
    """
    cid = inputs.get("campaign_id")
    tid = inputs.get("target_account_id")
    if not tid:
        # sometimes present inside nested target_account
        ta = _first_dict(inputs.get("target_account"))
        tid = ta.get("id") or ta.get("target_account_id")
    return cid, tid

# [MODIFIED] Use asyncio.to_thread for synchronous subprocess call
async def _run_blocking(cmd: List[str], cwd: Optional[str] = None, extra_env: Optional[Dict[str, str]] = None) -> int:
    """Execute a command in a separate thread to prevent blocking the event loop."""
    def _sync_run():
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        clean_cmd = []
        for arg in cmd:
            if arg is None:
                _print(f"[ERROR] Command contains None value: {cmd}")
                raise ValueError(f"Command argument cannot be None: {cmd}")
            clean_cmd.append(str(arg))
        _print(f"[orchestrator] exec: {' '.join(shlex.quote(c) for c in clean_cmd)}")
        proc = subprocess.Popen(clean_cmd, cwd=cwd, env=env)
        return proc.wait()

    return await asyncio.to_thread(_sync_run)

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
    parser.add_argument("--webhook-url", default=os.getenv("CALLBACK_URL", ""), help="Webhook URL to post status to")
    parser.add_argument("--webhook-secret", default=os.getenv("WEBHOOK_SECRET", ""), help="HMAC secret for webhook signature")
    parser.add_argument("--campaign-id", default=None, help="Override campaign_id (UUID)")
    parser.add_argument("--target-account-id", default=None, help="Override target_account_id (UUID)")
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID"), help="Run identifier propagated from Cloud Task")
    parser.add_argument("--job-id", default=None, help="(deprecated) Job/run identifier; prefer --run-id")
    args = parser.parse_args()

    # Normalize a stable job/run identifier
    job_id = args.run_id or args.job_id or os.environ.get("RUN_ID")

    # For Cloud Task / webhook flows we require a stable run_id
    if args.webhook_url and not job_id:
        raise SystemExit("[orchestrator] ERROR: run_id is required when webhook_url is set")

    # For purely local / non-webhook runs, allow auto-generated run_id
    if not job_id:
        job_id = str(uuid.uuid4())

    os.environ["RUN_ID"] = job_id
    _print(f"[orchestrator] Using RUN_ID={job_id}")

    if args.quiet:
        os.environ["QUIET"] = "1"

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs_raw = _load_json(args.inputs, default={}) or {}
    inputs = _normalize_inputs(inputs_raw)

    # --- Promote campaign-level configuration into env + inputs blob ---
    campaign_cfg = None
    try:
        # Prefer an explicit campaign_config blob if present (set by main.py)
        campaign_cfg = inputs.get("campaign_config")
        if not isinstance(campaign_cfg, dict):
            # Fallback: derive from embedded campaign record
            camp_rows = inputs.get("campaign") or []
            if isinstance(camp_rows, list) and camp_rows:
                camp0 = camp_rows[0] or {}
                settings_obj = camp0.get("settings") if isinstance(camp0.get("settings"), dict) else None
                campaign_cfg = settings_obj or camp0

        if isinstance(campaign_cfg, dict):
            # Map known JSON keys into env vars used by downstream steps
            key_mapping = [
                # Generic / web config
                ("web_query_pack", "WEB_QUERY_PACK"),
                ("web_max_results", "WEB_MAX_RESULTS"),
                ("evidence_strictness", "STEP5_STRICT"),

                # Target Account (TA) per-step model settings
                ("ta_step1_temperature", "TA_STEP1_TEMPERATURE"),
                ("ta_step1_max_output_tokens", "TA_STEP1_MAX_OUTPUT_TOKENS"),
                ("ta_step1_external_provisioned_qpm", "TA_STEP1_EXTERNAL_PROVISIONED_QPM"),
                ("ta_step1_external_safety_margin", "TA_STEP1_EXTERNAL_SAFETY_MARGIN"),
                ("ta_step1_external_concurrency", "TA_STEP1_EXTERNAL_CONCURRENCY"),

                ("ta_step2_temperature", "TA_STEP2_TEMPERATURE"),
                ("ta_step2_max_output_tokens", "TA_STEP2_MAX_OUTPUT_TOKENS"),

                ("ta_step3_temperature", "TA_STEP3_TEMPERATURE"),
                ("ta_step3_max_output_tokens", "TA_STEP3_MAX_OUTPUT_TOKENS"),

                ("ta_step4_temperature", "TA_STEP4_TEMPERATURE"),
                ("ta_step4_max_output_tokens", "TA_STEP4_MAX_OUTPUT_TOKENS"),

                ("ta_step5_temperature", "TA_STEP5_TEMPERATURE"),
                ("ta_step5_max_output_tokens", "TA_STEP5_MAX_OUTPUT_TOKENS"),

            ]
            for json_key, env_name in key_mapping:
                val = campaign_cfg.get(json_key)
                # Only set if there is a value and the env var is not already set
                if val not in (None, ""):
                    os.environ[env_name] = str(val)
                    logging.info(
                        "[orchestrator] Overriding env: %s=%r (from %s)",
                        env_name,
                        val,
                        json_key,
                    )
                logging.info("[orchestrator] %s=%s", env_name, os.environ.get(env_name))
            # Keep a normalized view on the inputs blob for any step that wants it
            inputs["campaign_config"] = campaign_cfg
    except Exception:
        logging.exception("[orchestrator] Failed to promote campaign config from inputs")

    # For persistence: pull IDs once
    campaign_id, target_account_id = _extract_ids_from_inputs(inputs)

    # Allow explicit overrides from CLI flags
    if args.campaign_id:
        campaign_id = args.campaign_id
    if args.target_account_id:
        target_account_id = args.target_account_id
    _print(f"[orchestrator] IDs: campaign_id={campaign_id}, target_account_id={target_account_id}")
    # Expose IDs to downstream components (collectors, etc.) via env for consistent logging context
    if campaign_id:
        os.environ["CAMPAIGN_ID"] = str(campaign_id)
    if target_account_id:
        os.environ["TARGET_ACCOUNT_ID"] = str(target_account_id)

    company, company_url, domain = _coalesce_company_context(inputs)
    if not isinstance(inputs, dict):
        inputs = {}
    if not domain and company_url:
        domain = _sanitize_domain(company_url)

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
            "campaign_id": campaign_id,
            "target_account_id": target_account_id,
            "run_id": job_id,
            "campaign_config": inputs.get("campaign_config"),
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

    # ----------------------------------------------------------------------------------
    # ---- Step 1: Web Evidence Grabber (ASYNCHRONOUS CALL)
    # ----------------------------------------------------------------------------------
    step1_json = out_dir / "step1_web_evidence.json"
    web_evidence: list = []
    used_inproc_step1 = False
    
    # [CRITICAL FIX] Use await on the run_step function
    if HAS_STEP1:
        try:
            web_evidence = await step1.run_step( # <-- AWAIT HERE
                company=company,
                company_url=company_url,
                domain=domain,
                max_results=int(os.getenv("WEB_MAX_RESULTS", "250")),
            ) or []
            # Keep artifact parity for downstream tools/fallbacks
            if KEEP_JSON:
                _save_json(step1_json, web_evidence)
            used_inproc_step1 = True
        except Exception as e:
            _print(f"[warn] Step 1 in-process failed, falling back: {e}")
            web_evidence = []
            used_inproc_step1 = False

    if not HAS_STEP1 or not used_inproc_step1:
        if Path("src/app/step1_evidence_grabber.py").exists():
            cmd = [
                py, "src/app/step1_evidence_grabber.py",
                "--company", company,
                "--company-url", company_url or "",
                "--out-json", str(step1_json),
                "--max-results", os.getenv("WEB_MAX_RESULTS", "500"),
            ]
            if domain:
                cmd.extend(["--domain", domain])
            
            # [IMPROVEMENT] Run subprocess in a thread
            rc = await _run_blocking(cmd, extra_env=child_env) 
            
            if rc != 0:
                _print(f"[warn] Step 1 exited with {rc}")
            web_evidence = _load_json(step1_json, default=[]) or []
        else:
            _print("[orchestrator] Step 1 script not found; skipping.")
            web_evidence = []
            if KEEP_JSON:
                _save_json(step1_json, web_evidence)

    await _append_event(
        session_service, session, "step1_done",
        "Collected grounded web evidence.",
        {"step1_count": len(web_evidence) if isinstance(web_evidence, list) else 0},
    )
    _save_step_json(target_account_id, "step1_web_evidence", web_evidence)
    # Persist Step 1 output into ADK session state
    session.state["step1:web_evidence"] = web_evidence

    # ----------------------------------------------------------------------------------
    # ---- Step 2: Evidence Harvester (SYNCHRONOUS CALL)
    # ----------------------------------------------------------------------------------
    step2_json = out_dir / "step2_problems.json"
    step2_md   = out_dir / "step2_problems.md"
    problems: dict = {}
    if HAS_STEP2:
        try:
            # Step 2 remains synchronous, so no await needed here
            problems = step2.run_step(company=company, raw_events=web_evidence) or {}
            if KEEP_JSON:
                _save_json(step2_json, problems)
        except Exception as e:
            _print(f"[warn] Step 2 in-process failed, falling back: {e}")
            problems = {}
    if not HAS_STEP2 or not problems:
        # Ensure the raw-events file exists for the subprocess path
        if not step1_json.exists():
            if KEEP_JSON:
                _save_json(step1_json, web_evidence)
        if Path("src/app/step2_evidence_harvester.py").exists():
            cmd = [
                py, "src/app/step2_evidence_harvester.py",
                "--company", company,
                "--raw-events", str(step1_json),
                "--out-json", str(step2_json),
                "--session-dir", str(out_dir),
            ]
            # [IMPROVEMENT] Run subprocess in a thread
            rc = await _run_blocking(cmd, extra_env=child_env)
            if rc != 0:
                _print(f"[warn] Step 2 exited with {rc}")
            problems = _load_json(step2_json, default={}) or {}
        else:
            _print("[orchestrator] Step 2 script not found; skipping.")
            problems = {}
            if KEEP_JSON:
                _save_json(step2_json, problems)
    await _append_event(
        session_service, session, "step2_done",
        "Generated evidenced problems.",
        {"step2_problem_count": len(problems.get("problems", [])) if isinstance(problems, dict) else 0},
    )
    _save_step_json(target_account_id, "step2_problems", problems)
    # Persist Step 2 output into ADK session state
    session.state["step2:problems"] = problems

    # ----------------------------------------------------------------------------------
    # ---- Step 3: Hypotheses Generator (SYNCHRONOUS CALL)
    # ----------------------------------------------------------------------------------
    step3_json = out_dir / "step3_hypotheses.json"
    step3_md   = out_dir / "step3_hypotheses.md"
    hypotheses: dict = {}
    time_window_val = inputs.get("time_window", "last 12-18 months")
    if HAS_STEP3:
        try:
            # Step 3 remains synchronous, so no await needed here
            evidence_index = problems
            hypotheses = step3.run_step(
                evidence_index=evidence_index,
                company=company,
                time_window=time_window_val,
                max_per_bucket=3,
            ) or {}
            if KEEP_JSON:
                _save_json(step3_json, hypotheses)
        except Exception as e:
            _print(f"[warn] Step 3 in-process failed, falling back: {e}")
            hypotheses = {}
    if not HAS_STEP3 or not hypotheses:
        # Ensure step2_json exists for fallback
        if not step2_json.exists():
            if KEEP_JSON:
                _save_json(step2_json, problems)
        if Path("src/app/step3_hypotheses_generator.py").exists():
            cmd3 = [
                py, "src/app/step3_hypotheses_generator.py",
                "--evidence", str(step2_json),
                "--company", company,
                "--time-window", time_window_val,
                "--out-json", str(step3_json),
                "--session-dir", str(out_dir),
            ]
            if KEEP_MD:
                cmd3.extend(["--out-md", str(step3_md)])
            
            # [IMPROVEMENT] Run subprocess in a thread
            rc = await _run_blocking(cmd3, extra_env=child_env)
            
            if rc != 0:
                _print(f"[warn] Step 3 exited with {rc}")
            hypotheses = _load_json(step3_json, default={}) or {}
        else:
            _print("[orchestrator] Step 3 script not found; skipping.")
            hypotheses = {}
            if KEEP_JSON:
                _save_json(step3_json, hypotheses)
    await _append_event(
        session_service, session, "step3_done",
        "Generated hypotheses seed.",
        {"step3_issue_count": len(hypotheses.get("issues", [])) if isinstance(hypotheses, dict) else 0},
    )
    _save_step_json(target_account_id, "step3_hypotheses", hypotheses)
    # Persist Step 3 output into ADK session state
    session.state["step3:hypotheses"] = hypotheses

    # ----------------------------------------------------------------------------------
    # ---- Step 4: Alignment (SYNCHRONOUS CALL)
    # ----------------------------------------------------------------------------------
    step4_json = out_dir / "step4_alignments.json"
    step4_md   = out_dir / "step4_alignments.md"
    taxonomy_path = args.campaign_challenges if args.campaign_challenges else None
    alignments: dict = {}

    if HAS_STEP4 and (hypotheses or {}).get("issues"):
        try:
            # Step 4 remains synchronous, so no await needed here
            taxonomy_obj = _load_json(taxonomy_path, default={}) if taxonomy_path else {}
            alignments = step4.run_step(
                issues=hypotheses,
                taxonomy=taxonomy_obj,
                top_k=3,
                company=company,
                time_window=time_window_val,
            ) or {}
            if KEEP_JSON:
                _save_json(step4_json, alignments)
        except Exception as e:
            _print(f"[warn] Step 4 in-process failed, falling back: {e}")
            alignments = {}
    if not HAS_STEP4 or not alignments:
        # Ensure step3_json exists for fallback
        if not step3_json.exists():
            if KEEP_JSON:
                _save_json(step3_json, hypotheses)
        cmd4 = [
            py, "src/app/step4_alignment.py",
            "--issues", str(step3_json),
            "--company", company,
            "--time-window", time_window_val,
            "--out-json", str(step4_json),
            "--session-dir", str(out_dir),
        ]
        if KEEP_MD:
            cmd4.extend(["--out-md", str(step4_md)])
        if taxonomy_path:
            cmd4.extend(["--taxonomy", taxonomy_path])
        if Path("src/app/step4_alignment.py").exists():
            
            # [IMPROVEMENT] Run subprocess in a thread
            rc = await _run_blocking(cmd4, extra_env=child_env)
            
            if rc != 0:
                _print(f"[warn] Step 4 exited with {rc}")
            alignments = _load_json(step4_json, default={}) or {}
        else:
            _print("[orchestrator] Step 4 script not found; skipping.")
            alignments = {}
            if KEEP_JSON:
                _save_json(step4_json, alignments)
    await _append_event(
        session_service, session, "step4_done",
        "Aligned issues to campaign challenges.",
        {"step4_alignment_count": len(alignments.get("alignments", [])) if isinstance(alignments, dict) else 0},
    )
    _save_step_json(target_account_id, "step4_alignments", alignments)
    # Persist Step 4 output into ADK session state
    session.state["step4:alignments"] = alignments

    # ----------------------------------------------------------------------------------
    # ---- Step 5: Compelling Events (SYNCHRONOUS CALL)
    # ----------------------------------------------------------------------------------
    step5_json = out_dir / "step5_compelling_events.json"
    step5_md   = out_dir / "step5_compelling_events.md"
    events: dict = {}
    strict_level = os.getenv("STEP5_STRICT", "medium")
    if HAS_STEP5:
        try:
            # Step 5 remains synchronous, so no await needed here
            problems_list = problems.get("problems", []) if isinstance(problems, dict) else []
            align_list = alignments.get("alignments", []) if isinstance(alignments, dict) else []
            events = step5.run_step(
                problems=problems_list,
                alignments=align_list,
                extra_evidence=web_evidence,
                strict=strict_level,
                max_sources=3,
                company=company,
            ) or {}
            if KEEP_JSON:
                _save_json(step5_json, events)
        except Exception as e:
            _print(f"[warn] Step 5 in-process failed, falling back: {e}")
            events = {}
    if not HAS_STEP5 or not events:
        # Ensure upstream artifacts exist for fallback
        if not step1_json.exists():
            if KEEP_JSON:
                _save_json(step1_json, web_evidence)
        if not step2_json.exists():
            if KEEP_JSON:
                _save_json(step2_json, problems)
        if not step4_json.exists():
            if KEEP_JSON:
                _save_json(step4_json, alignments)
        if Path("src/app/step5_compelling_events.py").exists():
            cmd5 = [
                py, "src/app/step5_compelling_events.py",
                "--issues", str(step2_json),
                "--alignments", str(step4_json),
                "--evidence", str(step1_json),
                "--company", company,
                "--out-json", str(step5_json),
                "--strict", strict_level,
            ]
            if KEEP_MD:
                cmd5.extend(["--out-md", str(step5_md)])
            
            # [IMPROVEMENT] Run subprocess in a thread
            rc = await _run_blocking(cmd5, extra_env=child_env)
            
            if rc != 0:
                _print(f"[warn] Step 5 exited with {rc}")
            events = _load_json(step5_json, default={}) or {}
        else:
            _print("[orchestrator] Step 5 script not found; skipping.")
            events = {}
            if KEEP_JSON:
                _save_json(step5_json, events)
    await _append_event(
        session_service, session, "step5_done",
        "Generated compelling events.",
        {"step5_event_count": len(events.get("compelling_events", [])) if isinstance(events, dict) else 0},
    )
    _save_step_json(target_account_id, "step5_compelling_events", events)
    # Persist Step 5 output into ADK session state
    session.state["step5:compelling_events"] = events

    # ---- Final summary (stdout + ADK event)
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

    try:
        step2_obj = _load_json(step2_json, default={}) or {}
        step3_obj = _load_json(step3_json, default={}) or {}
        step4_obj = _load_json(step4_json, default={}) or {}
        # Commented out original print lines for machine tails
        # print("__STEP2_JSON__:" + json.dumps(step2_obj, separators=(",", ":")))
        # print("__STEP3_JSON__:" + json.dumps(step3_obj, separators=(",", ":")))
        # print("__STEP4_JSON__:" + json.dumps(step4_obj, separators=(",", ":")))
        # print("__PIPELINE_JSON__:" + json.dumps({
        #     "step2_json": step2_obj,
        #     "step3_json": step3_obj,
        #     "step4_json": step4_obj
        # }, separators=(",", ":")))
    except Exception:
        pass

    # --- Post per-target-account progress webhook from run.py (executor owns reporting)
    if args.webhook_url:
        try:
            payload = {
                "run_id": os.environ.get("RUN_ID") or job_id,
                "campaign_id": campaign_id,
                "target_account_id": target_account_id,
                "status": "ok",
                "rc": 0,
                # lightweight counts for visibility
                "counts": {
                    "step1": len(web_evidence) if isinstance(web_evidence, list) else 0,
                    "step2": len((problems or {}).get("problems", [])) if isinstance(problems, dict) else 0,
                    "step3": len((hypotheses or {}).get("issues", [])) if isinstance(hypotheses, dict) else 0,
                    "step4": len((alignments or {}).get("alignments", [])) if isinstance(alignments, dict) else 0,
                    "step5": len((events or {}).get("compelling_events", [])) if isinstance(events, dict) else 0,
                },
            }
            wb_res = _post_webhook(args.webhook_url, args.webhook_secret, "ta_done", payload)
            #print(f"[WEBHOOK] result={wb_res}", flush=True)
        except Exception as e:
            print(f"[WEBHOOK] WARN: failed to post webhook: {type(e).__name__}: {e}", flush=True)

    # Optional cleanup of artifacts if requested
    if CLEAN_OUTPUTS:
        try:
            for p in [step1_json, step2_json, step3_json, step4_json, step5_json,
                      step2_md, step3_md, step4_md, step5_md]:
                try:
                    if p and Path(p).exists():
                        Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    # Final state snapshot (lightweight) to ensure ADK session has the latest view
    try:
        session.state.setdefault("summary", {})
        session.state["summary"].update({
            "step1_count": len(web_evidence) if isinstance(web_evidence, list) else 0,
            "step2_count": len((problems or {}).get("problems", [])) if isinstance(problems, dict) else 0,
            "step3_count": len((hypotheses or {}).get("issues", [])) if isinstance(hypotheses, dict) else 0,
            "step4_count": len((alignments or {}).get("alignments", [])) if isinstance(alignments, dict) else 0,
            "step5_count": len((events or {}).get("compelling_events", [])) if isinstance(events, dict) else 0,
        })
    except Exception:
        # Don't let state snapshot failures break the pipeline
        pass

    await _append_event(session_service, session, "pipeline_complete", "Pipeline complete.", {"status": "complete"})

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())