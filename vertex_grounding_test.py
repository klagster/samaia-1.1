# vertex_grounding_test.py
# End-to-end check that Gemini returns grounded links via Google Search.
# Works with 2.5-pro and 2.0-flash. Uses ADC, no gcloud/console required.

import os, json
from typing import List, Tuple
from google import genai

PROJECT  = os.getenv("GOOGLE_CLOUD_PROJECT", "portend-sam")
LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
MODEL    = os.getenv("MODEL_SEARCH", "gemini-2.0-flash")  # try 2.5-pro first; fallback to 2.0-flash if needed

def harvest_links(resp) -> List[Tuple[str, str]]:
    hits = []
    for cand in getattr(resp, "candidates", []) or []:
        gm = getattr(cand, "grounding_metadata", None)
        if not gm:
            continue
        # Check several buckets because SDK/model fields can vary
        buckets = [
            getattr(getattr(gm, "web", None), "sources", None),
            getattr(gm, "citations", None),
            getattr(gm, "search_results", None),
            getattr(gm, "supporting_content", None),
            getattr(gm, "auxiliary_attributions", None),
        ]
        for bucket in buckets:
            for s in (bucket or []):
                url = getattr(s, "uri", None) or getattr(s, "url", None)
                title = getattr(s, "title", "") or ""
                if url:
                    hits.append((title, url))
    # de-dup
    seen, out = set(), []
    for t, u in hits:
        if u in seen: 
            continue
        seen.add(u)
        out.append((t, u))
    return out

def main():
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    print(f"Project={PROJECT}  Region={LOCATION}  Model={MODEL}")

    prompt = (
        "Use the Google Search tool.\n"
        "Return ONLY JSON with an array 'sources', each item has 'title' and 'url'.\n"
        "Query: site:sec.gov 10-K Alphabet\n"
    )

    # Ask the model to *only* return JSON and make it deterministic.
    cfg = {
        "tools": [{"google_search": {}}],
        "response_mime_type": "application/json",
        "response_schema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                        "required": ["title", "url"],
                    },
                }
            },
            "required": ["sources"],
        },
        "temperature": 0,
        "max_output_tokens": 256,
    }

    resp = client.models.generate_content(
        model=MODEL,
        contents=[{"role": "user", "parts": [{"text": prompt}]}],
        config=cfg,
    )

    # Parse JSON body if model followed the schema
    json_sources = None
    try:
        for c in getattr(resp, "candidates", []) or []:
            parts = getattr(getattr(c, "content", None), "parts", None) or []
            for p in parts:
                t = getattr(p, "text", None)
                if t:
                    js = json.loads(t)
                    if isinstance(js, dict) and isinstance(js.get("sources"), list):
                        json_sources = [(s.get("title",""), s.get("url","")) for s in js["sources"]]
                        break
            if json_sources:
                break
    except Exception:
        pass

    if json_sources:
        print("✅ JSON sources (schema-compliant):")
        for title, url in json_sources[:8]:
            print(f" - {title or '(no title)'} -> {url}")
        return

    # Fallback: harvest from grounding metadata
    links = harvest_links(resp)
    if links:
        print("✅ Grounded sources (from grounding_metadata):")
        for t, u in links[:8]:
            print(f" - {t or '(no title)'} -> {u}")
    else:
        print("⚠️ No links found from 2.5-pro with schema. Try 2.0-flash:")
        print("   export MODEL_SEARCH=gemini-2.0-flash && python vertex_grounding_test.py")

if __name__ == "__main__":
    main()