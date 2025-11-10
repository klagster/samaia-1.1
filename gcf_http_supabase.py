# gcf_http_supabase.py
import os
import io
import json
import tempfile
import asyncio
from typing import Dict, Any

from pathlib import Path

from flask import Request, make_response
import functions_framework

import uuid
import logging
import traceback
import contextlib

# Helper: log-tee for streaming stdout/stderr to logs in real time
class _LogTee(io.TextIOBase):
    """
    Tee-like stream that writes to an in-memory buffer and also forwards complete
    lines to Python logging in real time. Use inside redirect_stdout/redirect_stderr.
    """
    def __init__(self, capture_buffer: io.StringIO, level: int = logging.INFO, prefix: str = ""):
        self._buf = capture_buffer
        self._level = level
        self._prefix = prefix
        self._partial = ""

    def write(self, s: str) -> int:
        # Always capture everything
        self._buf.write(s)
        # Accumulate and log complete lines
        self._partial += s
        if "\n" in self._partial:
            lines = self._partial.splitlines(keepends=False)
            # If the last chunk didn't end with newline, keep it as partial
            if self._partial.endswith("\n"):
                complete, self._partial = lines, ""
            else:
                complete, self._partial = lines[:-1], lines[-1]
            for line in complete:
                try:
                    logging.log(self._level, "%s%s", self._prefix, line)
                except Exception:
                    # Never blow up logging from write()
                    pass
        return len(s)

    def flush(self) -> None:
        # Flush any partial line as-is to logs (without forcing newline)
        if self._partial:
            try:
                logging.log(self._level, "%s%s", self._prefix, self._partial)
            except Exception:
                pass
            self._partial = ""
        # Ensure underlying buffer is flushed
        try:
            self._buf.flush()
        except Exception:
            pass

# Reuse your existing main
from src.app.run import main as samaia_main

# Supabase streamers
from src.integrations.supabase_source import stream_campaign_datasets

ALLOWED_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*")
RUN_TOKEN = os.getenv("RUN_TOKEN")  # if set, require X-Run-Token header to match

def _cors_headers() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin": ALLOWED_ORIGINS,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Run-Token",
        "Access-Control-Max-Age": "3600",
    }

def _resp(payload: Any, status: int = 200):
    # Always return valid JSON for dict/list payloads
    if isinstance(payload, (dict, list)):
        resp = make_response(json.dumps(payload), status)
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
    else:
        resp = make_response(payload, status)
    # CORS headers
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp

@functions_framework.http
def http_handler(request: Request):
    # CORS preflight
    if request.method == "OPTIONS":
        return _resp("", 204)

    if request.method != "POST":
        return _resp({"ok": False, "error": "Use POST with JSON body."}, 405)

    # Optional shared-secret auth
    if RUN_TOKEN:
        provided = request.headers.get("X-Run-Token", "")
        if provided != RUN_TOKEN:
            return _resp({"ok": False, "error": "Unauthorized"}, 401)

    body = request.get_json(silent=True) or {}
    campaign_id = body.get("campaign_id")
    target_account_id = body.get("target_account_id")
    # page_size: default 5000, clamp to [1, 5000]
    try:
        page_size = int(body.get("page_size", 5000))
    except (TypeError, ValueError):
        return _resp({"ok": False, "error": "page_size must be an integer"}, 400)
    page_size = max(1, min(5000, page_size))

    # Basic UUID validation
    def _is_uuid(x: str) -> bool:
        try:
            uuid.UUID(str(x))
            return True
        except Exception:
            return False

    if not (campaign_id and target_account_id and _is_uuid(campaign_id) and _is_uuid(target_account_id)):
        return _resp({"ok": False, "error": "Provide campaign_id and target_account_id as valid UUIDs."}, 400)

    # Stream records into a temp NDJSON that the CLI can consume.
    with tempfile.TemporaryDirectory() as tmpdir:
        inputs_ndjson = os.path.join(tmpdir, "inputs.ndjson")
        try:
            datasets = stream_campaign_datasets(campaign_id, target_account_id, page_size=page_size)

            # Write a single JSON object grouped by dataset name:
            # {
            #   "campaigns": [...],
            #   "target_accounts": [...],
            #   "clients": [...]
            # }
            merged_payload = {}
            for name, stream in datasets.items():
                try:
                    data_list = list(stream)
                    merged_payload[name] = data_list
                    logging.info(f"[GCF] Dataset '{name}' has {len(data_list)} records")
                except Exception as e:
                    logging.error(f"[GCF] Failed to process dataset '{name}': {e}")
                    merged_payload[name] = []

            # ---- Ensure required keys for run.py are present ----
            # Try to hydrate from needs_analysis_output if it was streamed.
            # We look for step_name == 'data_signal_mapping' and 'customer_challenges'
            # and copy their output_data into the expected top-level keys.
            if "needs_analysis_output" in merged_payload:
                try:
                    nao_rows = merged_payload["needs_analysis_output"]
                    # If upstream provided NAO rows as dicts, scan and extract
                    for row in nao_rows:
                        step = (row.get("step_name") or "").strip()
                        data = row.get("output_data")
                        if step == "data_signal_mapping" and "mapping_insights" not in merged_payload:
                            merged_payload["mapping_insights"] = data if data is not None else []
                        elif step == "customer_challenges" and "campaign_challenges" not in merged_payload:
                            merged_payload["campaign_challenges"] = data if data is not None else []
                except Exception:
                    # Non-fatal: fall through to defaults
                    pass

            # ---- Fallback: hydrate from streamed dataset keys when NAO isn't provided ----
            try:
                # mapping_insights from data_signal_mapping[0].output_data.mapping_insights
                if not merged_payload.get("mapping_insights"):
                    dsm_rows = merged_payload.get("data_signal_mapping") or []
                    if dsm_rows:
                        d0 = dsm_rows[0] or {}
                        od = d0.get("output_data") or {}
                        mi = od.get("mapping_insights")
                        if mi:
                            merged_payload["mapping_insights"] = mi
                            logging.info("[GCF] mapping_insights hydrated from data_signal_mapping")

                # campaign_challenges from customer_challenges[0].output_data.customer_challenges
                if not merged_payload.get("campaign_challenges"):
                    cc_rows = merged_payload.get("customer_challenges") or []
                    if cc_rows:
                        c0 = cc_rows[0] or {}
                        od = c0.get("output_data") or {}
                        ch = od.get("customer_challenges")
                        if ch:
                            merged_payload["campaign_challenges"] = ch
                            logging.info("[GCF] campaign_challenges hydrated from customer_challenges")

                # Optional: if a separate campaign_signals stream exists and contains mapping_insights, use as another fallback
                if not merged_payload.get("mapping_insights"):
                    cs_rows = merged_payload.get("campaign_signals") or []
                    if cs_rows:
                        s0 = cs_rows[0] or {}
                        od = s0.get("output_data") or {}
                        mi = od.get("mapping_insights")
                        if mi:
                            merged_payload["mapping_insights"] = mi
                            logging.info("[GCF] mapping_insights hydrated from campaign_signals")
            except Exception:
                # Non-fatal hydration failure
                logging.exception("[GCF] Failed to hydrate mapping_insights/campaign_challenges from dataset streams")
                pass

            # As a final guard, provide empty defaults if still missing.
            # run.py only requires the keys exist; empty payloads are acceptable for a dry run.
            merged_payload.setdefault("mapping_insights", [])
            merged_payload.setdefault("campaign_challenges", [])

            # ---- Derive company metadata + optional signal artifacts for run.py ----
            company_name = None
            company_url = None

            # If needs_analysis_output rows exist but weren't merged yet, scan them for
            # signal_categories/raw_events as optional inputs for run.py.
            try:
                # Some upstreams stream separate `needs_analysis_output` while we already copied
                # mapping_insights / campaign_challenges above. Here we additionally look for
                # `signal_categories` and `raw_events`.
                nao_rows = merged_payload.get("needs_analysis_output") or []
                for row in nao_rows:
                    data = row.get("output_data") or {}
                    if "signal_categories" in data and "signal_categories" not in merged_payload:
                        merged_payload["signal_categories"] = data.get("signal_categories") or []
                    if "raw_events" in data and "raw_events" not in merged_payload:
                        merged_payload["raw_events"] = data.get("raw_events") or []
            except Exception:
                pass

            # FIXED: Derive company_name and company_url strictly from Target Account with full debugging
            try:
                ta_rows = merged_payload.get("target_account") or []
                logging.info(f"[GCF] target_account type: {type(ta_rows)}, length: {len(ta_rows) if isinstance(ta_rows, list) else 'N/A'}")
                
                # Safety check: ensure it's actually a list
                if not isinstance(ta_rows, list):
                    logging.error(f"[GCF] target_account is not a list! Type: {type(ta_rows)}, Value: {str(ta_rows)[:200]}")
                    ta_rows = []
                
                if ta_rows:
                    ta0 = ta_rows[0] or {}
                    logging.info(f"[GCF] target_account[0] type: {type(ta0)}")
                    
                    if not isinstance(ta0, dict):
                        logging.error(f"[GCF] target_account[0] is not a dict! Type: {type(ta0)}, Value: {str(ta0)[:200]}")
                    else:
                        logging.info(f"[GCF] target_account[0] keys: {list(ta0.keys())}")
                        
                        # FIXED: Check account_name FIRST (Supabase schema uses account_name, not name)
                        company_name = ta0.get("account_name") or ta0.get("name")
                        logging.info(f"[GCF] Extracted company_name: {company_name}")
                        
                        # FIXED: Check company_website FIRST (Supabase schema uses company_website)
                        raw_url = (
                            ta0.get("company_website")
                            or ta0.get("homepage")
                            or ta0.get("website")
                            or ta0.get("url")
                        )
                        logging.info(f"[GCF] Extracted raw_url: {raw_url}")
                        
                        if not raw_url:
                            # Try to mine a URL from notes if present
                            notes = (ta0.get("notes") or "")
                            import re
                            m = re.search(r'(https?://[^\s]+|www\.[^\s]+)', notes)
                            if m:
                                raw_url = m.group(1)
                                logging.info(f"[GCF] Found URL in notes: {raw_url}")
                        
                        if raw_url:
                            raw_url = raw_url.strip()
                            if raw_url.startswith("www."):
                                raw_url = f"https://{raw_url}"
                            if raw_url.startswith(("http://", "https://")):
                                company_url = raw_url
                                logging.info(f"[GCF] Final company_url: {company_url}")
                else:
                    logging.warning("[GCF] target_account list is empty")
                # No campaign fallback by design
            except Exception as e:
                # Non-fatal
                logging.error(f"[GCF] Failed to extract company from target_account: {e}", exc_info=True)
                pass

            # FIXED: Clean up any stale env vars first
            if "COMPANY_NAME" in os.environ:
                del os.environ["COMPANY_NAME"]
            if "COMPANY_URL" in os.environ:
                del os.environ["COMPANY_URL"]

            # Export env for run.py to consume (only if we found valid values)
            if company_name:
                os.environ["COMPANY_NAME"] = company_name
                logging.info(f"[GCF] Set COMPANY_NAME={company_name}")
            else:
                logging.warning("[GCF] No company_name found in target_account")
                
            if company_url:
                os.environ["COMPANY_URL"] = company_url
                logging.info(f"[GCF] Set COMPANY_URL={company_url}")
            else:
                logging.warning("[GCF] No company_url found in target_account")

            # Ensure optional keys exist even if empty
            merged_payload.setdefault("signal_categories", [])
            merged_payload.setdefault("raw_events", [])

            with open(inputs_ndjson, "w", encoding="utf-8") as f:
                json.dump(merged_payload, f)

            # Write required CLI JSON files for run.py flags
            mapping_path = os.path.join(tmpdir, "mapping_insights.json")
            with open(mapping_path, "w", encoding="utf-8") as mf:
                json.dump(merged_payload.get("mapping_insights", []), mf)

            challenges_path = os.path.join(tmpdir, "campaign_challenges.json")
            with open(challenges_path, "w", encoding="utf-8") as cf:
                json.dump(merged_payload.get("campaign_challenges", []), cf)

            # Optional: write signal_categories.json and raw_events.json if present
            sigcats_path = None
            if merged_payload.get("signal_categories"):
                sigcats_path = os.path.join(tmpdir, "signal_categories.json")
                with open(sigcats_path, "w", encoding="utf-8") as sf:
                    json.dump(merged_payload["signal_categories"], sf)

            rawevents_path = None
            if merged_payload.get("raw_events"):
                rawevents_path = os.path.join(tmpdir, "raw_events.json")
                with open(rawevents_path, "w", encoding="utf-8") as rf:
                    json.dump(merged_payload["raw_events"], rf)

            logging.info("[GCF] Passing signal_categories=%s raw_events=%s", bool(sigcats_path), bool(rawevents_path))

            # Helpful server-side logs
            logging.info("[GCF] Derived COMPANY_NAME=%r COMPANY_URL=%r", os.environ.get("COMPANY_NAME"), os.environ.get("COMPANY_URL"))
            logging.info("[GCF] mapping_insights=%d, campaign_challenges=%d, signal_categories=%d, raw_events=%d",
                         len(merged_payload.get("mapping_insights") or []),
                         len(merged_payload.get("campaign_challenges") or []),
                         len(merged_payload.get("signal_categories") or []),
                         len(merged_payload.get("raw_events") or []))

            # ---- ADK Web Search grounding debug + guardrails ----
            web_query_pack = os.environ.get("WEB_QUERY_PACK")
            evidence_strictness = os.environ.get("EVIDENCE_STRICTNESS")
            web_max_results = os.environ.get("WEB_MAX_RESULTS")
            logging.info(
                "[GCF] WEB_QUERY_PACK=%r EVIDENCE_STRICTNESS=%r WEB_MAX_RESULTS=%r",
                web_query_pack, evidence_strictness, web_max_results
            )
            if web_query_pack:
                if not Path(web_query_pack).exists():
                    return _resp(
                        {
                            "ok": False,
                            "error": f"WEB_QUERY_PACK not found at {web_query_pack}. "
                                     "Set WEB_QUERY_PACK to a valid file (e.g., configs/web_queries.generic.json) "
                                     "or unset it to skip web grounding."
                        },
                        500,
                    )
            else:
                logging.warning("[GCF] WEB_QUERY_PACK is not set; run.py may skip ADK Google Web Search grounding.")

            # Reuse CLI main() with minimal argv
            import sys
            old_argv = list(sys.argv)
            sys.argv = [
                "run.py",
                "--inputs", inputs_ndjson,
                "--mapping-insights", mapping_path,
                "--campaign-challenges", challenges_path,
            ]
            if sigcats_path:
                sys.argv += ["--signal-categories", sigcats_path]
            if rawevents_path:
                sys.argv += ["--raw-events", rawevents_path]

            logging.info("[GCF] Invoking run.py with argv=%r", sys.argv)

            buf_out, buf_err = io.StringIO(), io.StringIO()
            stdout_tee = _LogTee(buf_out, level=logging.INFO, prefix="[RUN STDOUT] ")
            stderr_tee = _LogTee(buf_err, level=logging.WARNING, prefix="[RUN STDERR] ")
            try:
                with contextlib.redirect_stdout(stdout_tee), contextlib.redirect_stderr(stderr_tee):
                    asyncio.run(samaia_main())
            finally:
                # Ensure any partial fragments are pushed to logs
                try:
                    stdout_tee.flush()
                except Exception:
                    pass
                try:
                    stderr_tee.flush()
                except Exception:
                    pass
                sys.argv = old_argv

            output_text = buf_out.getvalue()
            error_text = buf_err.getvalue()
            # Avoid oversized responses: tail the last ~10KB
            def _tail(s: str, limit: int = 10_000) -> str:
                return s if len(s) <= limit else s[-limit:]

            return _resp(
                {
                    "ok": True,
                    "source": "supabase",
                    "campaign_id": campaign_id,
                    "target_account_id": target_account_id,
                    "output_tail": _tail(output_text),
                    "stderr_tail": _tail(error_text),
                },
                200,
            )
        except Exception as e:
            # Log full traceback server-side; return a concise error to client
            logging.exception("Unhandled error in http_handler")
            return _resp(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                500,
            )