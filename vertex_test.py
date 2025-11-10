# vertex_diag.py
# Diagnose Vertex AI access & grounding using ADC (no gcloud/console required).
# It probes common failure modes and prints human-readable conclusions.

import os
import json
import traceback
from typing import List, Tuple

def env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default

PROJECT  = env("GOOGLE_CLOUD_PROJECT", "portend-sam")     # your project
LOCATION = env("VERTEX_LOCATION", "us-central1")          # region
MODEL    = env("MODEL_SEARCH", "gemini-2.5-pro")          # set to the model you want

def summarize_exception(e: Exception) -> str:
    try:
        from google.genai.errors import ClientError
        if isinstance(e, ClientError):
            payload = getattr(e, "response_json", {}) or {}
            msg = payload.get("error", {}).get("message") or str(e)
            code = payload.get("error", {}).get("code")
            status = payload.get("error", {}).get("status")
            details = payload.get("error", {}).get("details") or []
            return f"{code} {status}: {msg} | details={json.dumps(details)[:500]}"
    except Exception:
        pass
    return f"{type(e).__name__}: {e}"

def classify_issue(err_text: str) -> str:
    t = err_text.lower()
    # API disabled
    if "service disabled" in t or "api has not been used" in t:
        return "❌ Vertex AI API appears DISABLED for this project. Ask an admin to enable aiplatform.googleapis.com."
    # IAM
    if "permission" in t and ("denied" in t or "iam_permission_denied" in t):
        if "aiplatform.endpoints.predict" in t:
            return ("❌ IAM: your ADC identity lacks Vertex AI predict permission.\n"
                    "Ask an admin to grant roles/aiplatform.user on the project.")
        return "❌ IAM: permission denied. You likely need roles/aiplatform.user."
    # Wrong model / region
    if "not found" in t or "resource not found" in t:
        return ("⚠️ Model/Region issue: the model name or region may be wrong/unavailable in this project.\n"
                f"Checked: model={MODEL}, region={LOCATION}. Try gemini-2.0-flash or confirm region.")
    # Network / transient
    if "deadline exceeded" in t or "timeout" in t or "temporarily unavailable" in t:
        return "⚠️ Transient or network issue. Retry; if persistent, check firewall/VPN."
    return "ℹ️ Unclassified error. See raw message above."

def harvest_links(resp) -> List[Tuple[str, str]]:
    hits: List[Tuple[str, str]] = []
    try:
        for cand in getattr(resp, "candidates", []) or []:
            gm = getattr(cand, "grounding_metadata", None)
            if not gm:
                continue
            web = getattr(gm, "web", None)
            if web:
                for s in getattr(web, "sources", []) or []:
                    url = getattr(s, "uri", None) or getattr(s, "url", None)
                    title = getattr(s, "title", "") or ""
                    if url:
                        hits.append((title, url))
            for cit in getattr(gm, "citations", []) or []:
                url = getattr(cit, "uri", None) or getattr(cit, "url", None)
                title = getattr(cit, "title", "") or ""
                if url:
                    hits.append((title, url))
            for r in getattr(gm, "search_results", []) or []:
                url = getattr(r, "uri", None) or getattr(r, "url", None)
                title = getattr(r, "title", "") or ""
                if url:
                    hits.append((title, url))
    except Exception:
        pass
    # de-dup
    seen, uniq = set(), []
    for t, u in hits:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((t, u))
    return uniq

def preview_text(resp, limit=300) -> str:
    try:
        for c in getattr(resp, "candidates", []) or []:
            parts = getattr(getattr(c, "content", None), "parts", None)
            if not parts:
                continue
            for p in parts:
                t = getattr(p, "text", None)
                if t:
                    return t[:limit]
    except Exception:
        pass
    return ""

def main():
    print(f"Project: {PROJECT}  Region: {LOCATION}  Model: {MODEL}")
    # 1) Basic client init (also validates ADC presence)
    try:
        from google import genai
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        print("✅ Client created via ADC")
    except Exception as e:
        print("❌ Could not initialize Vertex client with ADC")
        print(summarize_exception(e))
        print(classify_issue(summarize_exception(e)))
        return

    # 2) Simple generate_content with Google Search tool
    try:
        prompt = "Use Google Search to find recent sources about 'site:sec.gov 10-K Alphabet'. Cite sources."
        resp = client.models.generate_content(
            model=MODEL,
            contents=[{"role":"user","parts":[{"text": prompt}]}],
            config={"tools": [{"google_search": {}}], "max_output_tokens": 256},
        )
        links = harvest_links(resp)
        if links:
            print("✅ Grounded sources returned:")
            for t, u in links[:8]:
                print(f" - {t or '(no title)'} -> {u}")
        else:
            text = preview_text(resp)
            print("⚠️ No grounded links parsed. (This can be SDK/response-field variance or model behavior.)")
            if text:
                print("Model text preview:")
                print(text)
            else:
                print("No text content either. Consider trying a different model (e.g., gemini-2.0-flash).")
    except Exception as e:
        msg = summarize_exception(e)
        print("❌ Vertex generate_content failed.")
        print(msg)
        print(classify_issue(msg))
        # Optional: uncomment for full traceback during debugging
        # traceback.print_exc()

if __name__ == "__main__":
    main()