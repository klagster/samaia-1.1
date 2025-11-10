# gcf_http.py
import json
import io
import os
from typing import Any, Dict, Optional

from flask import Request, make_response
import functions_framework

# Import your existing runner
# Adjust the import to match your layout; here we assume:
#   src/app/run.py defines asyncio.run(main()) and helpers we can call.
import asyncio
from src.app.run import main as samaia_main

ALLOWED_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*")  # e.g. "https://yourapp.com,*"

def _cors_headers() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin": ALLOWED_ORIGINS,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "3600",
    }

def _bad_request(msg: str, status: int = 400):
    resp = make_response({"ok": False, "error": msg}, status)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp

def _parse_json_part(maybe_bytes_or_str: Any) -> Optional[Dict[str, Any]]:
    """
    Accepts an uploaded file, a JSON string, or None. Returns parsed dict or None.
    """
    if maybe_bytes_or_str is None:
        return None
    if hasattr(maybe_bytes_or_str, "read"):  # Werkzeug FileStorage
        data = maybe_bytes_or_str.read()
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None
    if isinstance(maybe_bytes_or_str, (bytes, bytearray)):
        try:
            return json.loads(maybe_bytes_or_str.decode("utf-8"))
        except Exception:
            return None
    if isinstance(maybe_bytes_or_str, str):
        try:
            return json.loads(maybe_bytes_or_str)
        except Exception:
            return None
    return None

def _collect_inputs_from_request(req: Request) -> Dict[str, Any]:
    """
    Accept JSON files via multipart/form-data OR raw JSON body.
    Supported keys (as files or inline JSON strings):
      - inputs                    (Step B user inputs JSON)
      - mapping_insights          (Step A)
      - signal_categories         (Step A)
      - raw_events                (Step A)
      - campaign_challenges       (Step C)
    """
    payload: Dict[str, Any] = {
        "args": {
            "inputs": None,
            "mapping_insights": None,
            "signal_categories": None,
            "raw_events": None,
            "campaign_challenges": None,
        }
    }

    if req.content_type and "multipart/form-data" in req.content_type:
        # Files first
        for key in payload["args"].keys():
            if key in req.files:
                payload["args"][key] = _parse_json_part(req.files[key])
            elif key in req.form:
                payload["args"][key] = _parse_json_part(req.form.get(key))
    else:
        # Try raw JSON body
        try:
            body = req.get_json(silent=True) or {}
        except Exception:
            body = {}
        for key in payload["args"].keys():
            v = body.get(key)
            payload["args"][key] = _parse_json_part(v) if isinstance(v, (str, bytes)) else v

    return payload

@functions_framework.http
def http_handler(request: Request):
    # Handle preflight
    if request.method == "OPTIONS":
        resp = make_response("", 204)
        for k, v in _cors_headers().items():
            resp.headers[k] = v
        return resp

    if request.method != "POST":
        return _bad_request("Use POST with JSON files (multipart/form-data) or JSON body.", 405)

    bundle = _collect_inputs_from_request(request)
    args = bundle["args"]

    # Minimal validation: require at least the Step B 'inputs'
    if not args["inputs"]:
        return _bad_request("Missing 'inputs' JSON (upload a file named 'inputs' or include a JSON field).")

    # Write temp files so we can call your CLI-style main via argv parity if desired.
    # Your run.py already accepts --inputs and optional Step A/C files; we’ll reuse that.
    import tempfile, os, json

    tmpdir = tempfile.mkdtemp()
    def _dump_if_present(name: str, data: Optional[Dict[str, Any]]) -> Optional[str]:
        if not data:
            return None
        p = os.path.join(tmpdir, f"{name}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return p

    inputs_p             = _dump_if_present("inputs", args["inputs"])
    mapping_insights_p   = _dump_if_present("mapping_insights", args["mapping_insights"])
    signal_categories_p  = _dump_if_present("signal_categories", args["signal_categories"])
    raw_events_p         = _dump_if_present("raw_events", args["raw_events"])
    campaign_challenges_p= _dump_if_present("campaign_challenges", args["campaign_challenges"])

    # Build fake argv for argparser in run.py
    import sys
    old_argv = list(sys.argv)
    sys.argv = ["run.py"]
    if inputs_p:            sys.argv += ["--inputs", inputs_p]
    if mapping_insights_p:  sys.argv += ["--mapping-insights", mapping_insights_p]
    if signal_categories_p: sys.argv += ["--signal-categories", signal_categories_p]
    if raw_events_p:        sys.argv += ["--raw-events", raw_events_p]
    if campaign_challenges_p: sys.argv += ["--campaign-challenges", campaign_challenges_p]

    # Capture stdout so we can return Markdown + JSON in the response
    import contextlib, io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            # run.py’s main() is async — we call it through asyncio
            asyncio.run(samaia_main())
        output_text = buf.getvalue()
    except SystemExit as e:
        output_text = buf.getvalue() + f"\n[SystemExit] {e}\n"
    except Exception as e:
        return _bad_request(f"Processing error: {e}", 500)
    finally:
        sys.argv = old_argv

    # Respond with text (Markdown + fenced JSON at the end). You could also try
    # to extract the last fenced JSON here and return an object, but returning
    # the whole text is useful for the browser UI.
    resp = make_response({"ok": True, "output": output_text}, 200)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp