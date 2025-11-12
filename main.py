# gcf_http_supabase.py
import os
import io
import json
import tempfile
import asyncio
from typing import Dict, Any

import hmac
import hashlib
import urllib.request
import urllib.error
import time

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
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # default shared secret for signing callbacks

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

# --- Webhook helpers --------------------------------------------------------

def _sign_hmac_sha256(secret: str, body: bytes) -> str:
    try:
        mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
        return mac.hexdigest()
    except Exception:
        return ""

def _post_webhook(url: str, payload: Dict[str, Any], *, event_type: str, run_id: str,
                  secret: str, max_retries: int = 5, base_delay: float = 1.0) -> Dict[str, Any]:
    """POST JSON to webhook URL with HMAC signature and simple retries.
    Returns a dict with status and last error if any (non-fatal to caller)."""
    delivery_id = str(uuid.uuid4())
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Event-Type": event_type,
        "X-Run-Id": run_id,
        "X-Delivery-Id": delivery_id,
    }
    if secret:
        sig = _sign_hmac_sha256(secret, body)
        if sig:
            headers["X-Signature"] = f"sha256={sig}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.getcode()
                return {"ok": 200 <= status < 300, "status": status, "delivery_id": delivery_id}
        except urllib.error.HTTPError as e:
            last_err = f"HTTPError {e.code}: {e.read().decode('utf-8', 'ignore')[:500]}"
        except urllib.error.URLError as e:
            last_err = f"URLError: {getattr(e, 'reason', e)}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        # backoff
        time.sleep(base_delay * (2 ** attempt))
    return {"ok": False, "error": last_err, "delivery_id": delivery_id}

 # --- Helper to process a single target_account for a campaign ---
async def _process_one_target_account(campaign_id: str, target_account_id: str, page_size: int) -> Dict[str, Any]:
    """Run the pipeline for one (campaign_id, target_account_id) pair and return a compact result dict.
    NOTE: This mirrors the previous single-account flow from http_handler."""
    with tempfile.TemporaryDirectory() as tmpdir:
        inputs_ndjson = os.path.join(tmpdir, "inputs.ndjson")
        try:
            datasets = stream_campaign_datasets(campaign_id, target_account_id, page_size=page_size)

            # Build merged payload as before
            merged_payload = {}
            for name, stream in datasets.items():
                try:
                    data_list = list(stream)
                    merged_payload[name] = data_list
                    logging.info(f"[GCF] Dataset '{name}' has {len(data_list)} records")
                except Exception as e:
                    logging.error(f"[GCF] Failed to process dataset '{name}': {e}")
                    merged_payload[name] = []

            # Hydration logic (mapping_insights/campaign_challenges) — unchanged
            if "needs_analysis_output" in merged_payload:
                try:
                    nao_rows = merged_payload["needs_analysis_output"]
                    for row in nao_rows:
                        step = (row.get("step_name") or "").strip()
                        data = row.get("output_data")
                        if step == "data_signal_mapping" and "mapping_insights" not in merged_payload:
                            merged_payload["mapping_insights"] = data if data is not None else []
                        elif step == "customer_challenges" and "campaign_challenges" not in merged_payload:
                            merged_payload["campaign_challenges"] = data if data is not None else []
                except Exception:
                    pass

            try:
                if not merged_payload.get("mapping_insights"):
                    dsm_rows = merged_payload.get("data_signal_mapping") or []
                    if dsm_rows:
                        d0 = dsm_rows[0] or {}
                        od = d0.get("output_data") or {}
                        mi = od.get("mapping_insights")
                        if mi:
                            merged_payload["mapping_insights"] = mi
                            logging.info("[GCF] mapping_insights hydrated from data_signal_mapping")
                if not merged_payload.get("campaign_challenges"):
                    cc_rows = merged_payload.get("customer_challenges") or []
                    if cc_rows:
                        c0 = cc_rows[0] or {}
                        od = c0.get("output_data") or {}
                        ch = od.get("customer_challenges")
                        if ch:
                            merged_payload["campaign_challenges"] = ch
                            logging.info("[GCF] campaign_challenges hydrated from customer_challenges")
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
                logging.exception("[GCF] Failed to hydrate mapping_insights/campaign_challenges from dataset streams")
                pass

            merged_payload.setdefault("mapping_insights", [])
            merged_payload.setdefault("campaign_challenges", [])

            # Derive company meta strictly from target_account
            company_name = None
            company_url = None
            try:
                ta_rows = merged_payload.get("target_account") or []
                logging.info(f"[GCF] target_account type: {type(ta_rows)}, length: {len(ta_rows) if isinstance(ta_rows, list) else 'N/A'}")
                if not isinstance(ta_rows, list):
                    logging.error(f"[GCF] target_account is not a list! Type: {type(ta_rows)}, Value: {str(ta_rows)[:200]}")
                    ta_rows = []
                if ta_rows:
                    ta0 = ta_rows[0] or {}
                    logging.info(f"[GCF] target_account[0] keys: {list(ta0.keys())}")
                    company_name = ta0.get("account_name") or ta0.get("name")
                    raw_url = (
                        ta0.get("company_website")
                        or ta0.get("homepage")
                        or ta0.get("website")
                        or ta0.get("url")
                    )
                    if not raw_url:
                        notes = (ta0.get("notes") or "")
                        import re
                        m = re.search(r'(https?://[^\s]+|www\.[^\s]+)', notes)
                        if m:
                            raw_url = m.group(1)
                    if raw_url:
                        raw_url = raw_url.strip()
                        if raw_url.startswith("www."):
                            raw_url = f"https://{raw_url}"
                        if raw_url.startswith(("http://", "https://")):
                            company_url = raw_url
            except Exception as e:
                logging.error(f"[GCF] Failed to extract company from target_account: {e}", exc_info=True)
                pass

            for k in ("COMPANY_NAME", "COMPANY_URL"):
                if k in os.environ:
                    del os.environ[k]
            if company_name:
                os.environ["COMPANY_NAME"] = company_name
            if company_url:
                os.environ["COMPANY_URL"] = company_url

            merged_payload.setdefault("signal_categories", [])
            merged_payload.setdefault("raw_events", [])

            with open(inputs_ndjson, "w", encoding="utf-8") as f:
                json.dump(merged_payload, f)

            mapping_path = os.path.join(tmpdir, "mapping_insights.json")
            with open(mapping_path, "w", encoding="utf-8") as mf:
                json.dump(merged_payload.get("mapping_insights", []), mf)

            challenges_path = os.path.join(tmpdir, "campaign_challenges.json")
            with open(challenges_path, "w", encoding="utf-8") as cf:
                json.dump(merged_payload.get("campaign_challenges", []), cf)

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

            # Debug env
            logging.info("[GCF] Derived COMPANY_NAME=%r COMPANY_URL=%r", os.environ.get("COMPANY_NAME"), os.environ.get("COMPANY_URL"))
            logging.info("[GCF] mapping_insights=%d, campaign_challenges=%d, signal_categories=%d, raw_events=%d",
                         len(merged_payload.get("mapping_insights") or []),
                         len(merged_payload.get("campaign_challenges") or []),
                         len(merged_payload.get("signal_categories") or []),
                         len(merged_payload.get("raw_events") or []))

            web_query_pack = os.environ.get("WEB_QUERY_PACK")
            evidence_strictness = os.environ.get("EVIDENCE_STRICTNESS")
            web_max_results = os.environ.get("WEB_MAX_RESULTS")
            logging.info(
                "[GCF] WEB_QUERY_PACK=%r EVIDENCE_STRICTNESS=%r WEB_MAX_RESULTS=%r",
                web_query_pack, evidence_strictness, web_max_results
            )
            if web_query_pack and not Path(web_query_pack).exists():
                return {
                    "ok": False,
                    "error": f"WEB_QUERY_PACK not found at {web_query_pack}",
                    "target_account_id": target_account_id,
                }

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

            buf_out, buf_err = io.StringIO(), io.StringIO()
            stdout_tee = _LogTee(buf_out, level=logging.INFO, prefix="[RUN STDOUT] ")
            stderr_tee = _LogTee(buf_err, level=logging.WARNING, prefix="[RUN STDERR] ")
            try:
                with contextlib.redirect_stdout(stdout_tee), contextlib.redirect_stderr(stderr_tee):
                    await samaia_main()
            finally:
                try:
                    stdout_tee.flush()
                except Exception:
                    pass
                try:
                    stderr_tee.flush()
                except Exception:
                    pass
                sys.argv = old_argv

            def _tail(s: str, limit: int = 10_000) -> str:
                return s if len(s) <= limit else s[-limit:]

            return {
                "ok": True,
                "target_account_id": target_account_id,
                "output_tail": _tail(buf_out.getvalue()),
                "stderr_tail": _tail(buf_err.getvalue()),
            }
        except Exception as e:
            logging.exception("Unhandled error in _process_one_target_account")
            return {"ok": False, "target_account_id": target_account_id, "error": f"{type(e).__name__}: {e}"}

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
    callback_url = body.get("callback_url")
    webhook_secret = body.get("webhook_secret") or WEBHOOK_SECRET
    run_id = str(uuid.uuid4())
    campaign_id = body.get("campaign_id")
    target_account_id = body.get("target_account_id")  # optional for back-compat

    # Basic UUID validation helper
    def _is_uuid(x: str) -> bool:
        try:
            uuid.UUID(str(x))
            return True
        except Exception:
            return False

    if not (campaign_id and _is_uuid(campaign_id)):
        return _resp({"ok": False, "error": "Provide campaign_id as a valid UUID."}, 400)

    # page_size: default 5000, clamp to [1, 5000]
    try:
        page_size = int(body.get("page_size", 5000))
    except (TypeError, ValueError):
        return _resp({"ok": False, "error": "page_size must be an integer"}, 400)
    page_size = max(1, min(5000, page_size))

    # If target_account_id provided, keep previous single-account behavior
    if target_account_id:
        if not _is_uuid(target_account_id):
            return _resp({"ok": False, "error": "target_account_id must be a valid UUID if provided."}, 400)
        # Run one
        result = asyncio.run(_process_one_target_account(campaign_id, target_account_id, page_size))
        status = 200 if result.get("ok") else 500
        if callback_url:
            try:
                _post_webhook(
                    callback_url,
                    {
                        "run_id": run_id,
                        "campaign_id": campaign_id,
                        "target_account_id": target_account_id,
                        "status": "ok" if result.get("ok") else "failed",
                        "rc": 0 if result.get("ok") else 1,
                        "output_tail": result.get("output_tail"),
                        "stderr_tail": result.get("stderr_tail"),
                    },
                    event_type="ta_progress",
                    run_id=run_id,
                    secret=webhook_secret,
                )
            except Exception:
                logging.exception("[GCF] webhook progress callback failed (single-account)")
        return _resp({"ok": bool(result.get("ok")), "campaign_id": campaign_id, "run_id": run_id, "results": [result]}, status)

    # Otherwise: enumerate ALL target accounts for the campaign and run them all
    try:
        try:
            # New helper expected in supabase_source.py
            from src.integrations.supabase_source import list_target_accounts_for_campaign
        except Exception:
            return _resp({
                "ok": False,
                "error": "Supabase source missing list_target_accounts_for_campaign(campaign_id, page_size). Please add it and redeploy.",
            }, 500)

        ta_rows = list_target_accounts_for_campaign(campaign_id, page_size=page_size)
        if not isinstance(ta_rows, list):
            return _resp({"ok": False, "error": "list_target_accounts_for_campaign returned non-list"}, 500)
        if not ta_rows:
            return _resp({"ok": True, "campaign_id": campaign_id, "results": [], "message": "No target accounts found for campaign."}, 200)

        results: list[dict] = []
        started = time.time()
        ok_count = 0
        fail_count = 0
        # Run sequentially; could be parallelized later with asyncio.gather
        for row in ta_rows:
            ta_id = row.get("id") or row.get("target_account_id")
            if not ta_id or not _is_uuid(ta_id):
                logging.warning("[GCF] Skipping TA with missing/invalid id: %r", row)
                continue
            logging.info("[GCF] Processing target_account_id=%s", ta_id)
            r = asyncio.run(_process_one_target_account(campaign_id, ta_id, page_size))
            results.append(r)
            if r.get("ok"):
                ok_count += 1
            else:
                fail_count += 1
            if callback_url:
                try:
                    _post_webhook(
                        callback_url,
                        {
                            "run_id": run_id,
                            "campaign_id": campaign_id,
                            "target_account_id": ta_id,
                            "status": "ok" if r.get("ok") else "failed",
                            "rc": 0 if r.get("ok") else 1,
                            "elapsed_ms": int((time.time() - started) * 1000),
                            "output_tail": r.get("output_tail"),
                            "stderr_tail": r.get("stderr_tail"),
                        },
                        event_type="ta_progress",
                        run_id=run_id,
                        secret=webhook_secret,
                    )
                except Exception:
                    logging.exception("[GCF] webhook progress callback failed (multi-account)")

        if callback_url:
            try:
                duration_ms = int((time.time() - started) * 1000)
                _post_webhook(
                    callback_url,
                    {
                        "run_id": run_id,
                        "campaign_id": campaign_id,
                        "summary": {
                            "total": len(results),
                            "ok": ok_count,
                            "failed": fail_count,
                            "duration_ms": duration_ms,
                        },
                        "results": [
                            {"target_account_id": rr.get("target_account_id"), "status": "ok" if rr.get("ok") else "failed", "rc": 0 if rr.get("ok") else 1}
                            for rr in results
                        ],
                    },
                    event_type="ta_done",
                    run_id=run_id,
                    secret=webhook_secret,
                )
            except Exception:
                logging.exception("[GCF] webhook final callback failed")

        ok_all = all(rt.get("ok") for rt in results) if results else True
        status = 200 if ok_all else 207  # 207 Multi-Status-like semantics via 200 with mixed ok
        return _resp({"ok": ok_all, "campaign_id": campaign_id, "run_id": run_id, "results": results}, status)
    except Exception as e:
        logging.exception("[GCF] Error while iterating target accounts")
        return _resp({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)