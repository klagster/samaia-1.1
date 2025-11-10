#!/usr/bin/env python3
"""
preview_queries.py

Execute queries from a web_queries JSON config using Vertex AI's Google Search tool
and print summarized sources per category.

Requirements:
  pip install google-genai

Env:
  GOOGLE_CLOUD_PROJECT=...
  VERTEX_LOCATION=us-central1 (or your region)
  MODEL_SEARCH=gemini-2.0-flash-001 (or another model that supports google_search)

Usage:
  python preview_queries.py configs/web_queries.generic.json --company "Equinix" --domain "equinix.com"
  python preview_queries.py configs/web_queries.generic.json --company "Eli Lilly And Company" --domain "lilly.com" --category "Market, News & Analyst Reports" --max-per-cat 5
"""

import argparse, json, pathlib, re, sys, datetime
from typing import Any, Dict, List, Tuple, Optional

def load_concat_json(path: pathlib.Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    objs, i = [], 0
    while i < len(text):
        while i < len(text) and text[i].isspace(): i += 1
        if i >= len(text): break
        if text[i] != '{': raise ValueError(f"Expected '{{' at {i}")
        depth = 0; j = i; ins = False; esc = False
        while j < len(text):
            c = text[j]
            if ins:
                if esc: esc = False
                elif c == '\\': esc = True
                elif c == '"': ins = False
            else:
                if c == '"': ins = True
                elif c == '{': depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
            j += 1
        objs.append(json.loads(text[i:j]))
        i = j
    if not objs: raise ValueError("No JSON objects found.")
    return objs

def choose_version(blocks: List[Dict[str, Any]], version: Optional[str]) -> Dict[str, Any]:
    if version is None:
        return sorted(blocks, key=lambda o: float(o.get("version", -1)))[-1]
    for o in blocks:
        if str(o.get("version")) == str(version): return o
    raise ValueError(f"Version {version} not found. Available: {[o.get('version') for o in blocks]}")

def expand(template: str, company: str, domain: str) -> str:
    return (template
            .replace("{{company}}", company)
            .replace("{{domain}}", domain))

def extract_first_json_obj(text: str) -> Optional[Dict[str, Any]]:
    # The model sometimes returns duplicated JSON blocks. Grab the first valid object.
    m = re.search(r'\{[\s\S]*\}', text)
    if not m: return None
    try:
        return json.loads(m.group(0))
    except Exception:
        # Try to be resilient: find the first balanced object by braces
        depth = 0; start = None
        for i, ch in enumerate(text):
            if ch == '{':
                if start is None: start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start:i+1])
                    except Exception:
                        start = None
        return None

def run_search(queries: List[str], model_name: str, project: str, location: str, recency_days: Optional[int], max_results: int) -> List[Tuple[str, List[Dict[str, str]]]]:
    from google import genai
    client = genai.Client(vertexai=True, project=project, location=location)
    results = []
    for q in queries:
        # Ask the model to use Google Search and return JSON {sources:[{title,url}]}
        prompt = (
            f'Use Google Search. Return ONLY JSON {{sources:[{{title,url}}]}}. '
            f'Find up to {max_results} results for: {q}'
        )
        # We cannot force recency directly, but many configs bake it into the query (e.g., "past year").
        # If recency_days provided, append a time hint:
        if recency_days:
            prompt += f' Limit to the last {recency_days} days if possible.'
        resp = client.models.generate_content(
            model=model_name,
            contents=[{"role": "user", "parts":[{"text": prompt}]}],
            config={
                "tools":[{"google_search":{}}],
                "response_mime_type":"application/json",
                "max_output_tokens":768,
                "temperature":0
            }
        )
        text = getattr(resp, "text", None) or str(resp)
        obj = extract_first_json_obj(text) or {}
        sources = obj.get("sources") or []
        # Normalize schema: ensure title/url keys present
        clean = []
        for s in sources:
            t = (s.get("title") or "").strip()
            u = (s.get("url") or "").strip()
            if t and u:
                clean.append({"title": t, "url": u})
        results.append((q, clean[:max_results]))
    return results

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="Path to web_queries JSON")
    ap.add_argument("--version", help="Version to use (default: highest)")
    ap.add_argument("--company", required=True, help="Company name (for {{company}})")
    ap.add_argument("--domain", required=True, help="Company domain (for {{domain}})")
    ap.add_argument("--category", help="Only run a specific category by name")
    ap.add_argument("--max-per-cat", type=int, default=6, help="Max results per query (cap)")
    ap.add_argument("--override-recency", type=int, help="Override recency_days for all categories")
    ap.add_argument("--project", default=None, help="GCP project (default: env GOOGLE_CLOUD_PROJECT)")
    ap.add_argument("--location", default=None, help="Vertex location (default: env VERTEX_LOCATION)")
    ap.add_argument("--model", default=None, help="Search-capable model (default: env MODEL_SEARCH)")
    args = ap.parse_args()

    import os
    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = args.location or os.environ.get("VERTEX_LOCATION", "us-central1")
    model = args.model or os.environ.get("MODEL_SEARCH", "gemini-2.0-flash-001")
    if not project:
        print("ERROR: set GOOGLE_CLOUD_PROJECT or pass --project", file=sys.stderr); sys.exit(2)

    blocks = load_concat_json(pathlib.Path(args.file))
    cfg = choose_version(blocks, args.version)

    defaults = cfg.get("defaults", {})
    def_recency = defaults.get("recency_days")
    def_max = int(defaults.get("max_results", args.max_per_cat))

    categories = cfg.get("categories", [])
    if args.category:
        categories = [c for c in categories if c.get("name") == args.category]
        if not categories:
            print(f"Category '{args.category}' not found.", file=sys.stderr)
            sys.exit(1)

    # Run
    for cat in categories:
        cname = cat.get("name", "(unnamed)")
        recency = args.override_recency if args.override_recency is not None else cat.get("recency_days", def_recency)
        maxres  = int(cat.get("max_results", def_max))
        queries = [expand(q, args.company, args.domain) for q in cat.get("queries", [])]
        print(f"\n=== Category: {cname} (recency_days={recency}, max_results={maxres}) ===")
        if not queries:
            print("  (no queries)")
            continue
        pairs = run_search(queries, model, project, location, recency, maxres)
        for q, sources in pairs:
            print(f"\n• Query: {q}")
            if not sources:
                print("  -> (no results)")
                continue
            for i, s in enumerate(sources, 1):
                print(f"  {i:>2}. {s['title']}\n      {s['url']}")

if __name__ == "__main__":
    main()
    