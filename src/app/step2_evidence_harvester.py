#!/usr/bin/env python3
"""
Step 2: Evidence Harvester (session-aware)
- Input (from Step 1): JSON or NDJSON containing an 'events' array, or newline-delimited event records.
- Output: {"company": "...", "generated_at": "...", "evidence":[{ id, title, url, date_iso, snippet, category, publisher, query, confidence, source_type }]}

Orchestrator compatibility:
  * Accepts `--session-dir` to auto-place outputs when `--out/--out-json` not provided.
  * Emits machine-readable tails:
      __STEP2_EVIDENCE_PATH__:<abs_path>
      __STEP2_EVIDENCE_COUNT__:<int>

Usage:
  python src/app/step2_evidence_harvester.py \
    --company "Equinix" \
    --raw-events .outputs/equinix_raw_events.json \
    --out .outputs/equinix_evidence_index.json

Optional:
  --raw-events-ndjson .outputs/equinix_raw_events.ndjson
  --out-ndjson .outputs/equinix_evidence_index.ndjson
  --session-dir .outputs/sessions/equinix_2025-11-04
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional
import argparse, json, hashlib, re, sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def _read_events_any(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"raw events not found: {path}")
    text = p.read_text(encoding="utf-8").strip()

    # Try JSON with {"events":[...]}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "events" in obj and isinstance(obj["events"], list):
            return obj["events"]
        # If it's a single list of hits
        if isinstance(obj, list):
            return obj
    except Exception:
        pass

    # Try NDJSON
    evts: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                evts.append(rec)
        except Exception:
            continue
    return evts


CATEGORY_HINTS = {
    "Corporate & Official Sources": [
        "newsroom", "press", "news", "media", "update", "blog", "insights",
        "/news", "/press"
    ],
    "Financial Filings & Capital Markets": [
        "investors.", "ir.", "10-k", "10q", "10-q", "sec", "earnings",
        "presentations", "events", "financial", "filings"
    ],
    "Jobs & Hiring Signals": [
        "careers.", "/careers", "/jobs", "linkedin.com/jobs",
        "myworkdayjobs.com", "greenhouse.io", "lever.co"
    ],
    "Market, News & Analyst Reports": [
        "reuters.com", "bloomberg.com", "businesswire.com",
        "prnewswire.com", "globenewswire.com", "yahoo.com",
        "market", "analyst", "coverage", "rating"
    ],
    "Risk, Compliance & Security": [
        "breach", "incident", "outage", "vulnerability", "gdpr",
        "soc 2", "iso 27001", "fine", "sanction", "regulator",
        "enforcement"
    ],
    "Technology & Operations (General)": [
        "cloud", "migration", "legacy", "data platform", "automation",
        "modernization", "zero trust", "sase"
    ],
    "Technology & Operations (Data Center / AI)": [
        "colocation", "interconnection", "ibx", "xscale", "metal",
        "fabric", "gpu", "nvidia", "h100", "b200", "cluster",
        "liquid cooling", "pue", "mw", "substation"
    ],
    "Sustainability & Energy": [
        "renewable", "ppa", "power purchase", "solar", "wind",
        "geothermal", "scope 1", "scope 2", "scope 3", "sbti",
        "science-based targets", "esg"
    ],
    "Customer & Product": [
        "case study", "customer story", "testimonial", "reference",
        "product", "platform", "roadmap", "deprecate", "end-of-life",
        "launch"
    ],
    "M&amp;A, Geography &amp; Expansion": [
        "acquires", "acquisition", "divests", "spin-off", "opens",
        "opening", "expands", "expansion", "greenfield", "brownfield",
        "campus", "region", "country"
    ],
}

PUBLISHER_WEIGHTS = {
    "reuters.com": 1.0,
    "bloomberg.com": 1.0,
    "businesswire.com": 0.9,
    "prnewswire.com": 0.9,
    "globenewswire.com": 0.9,
    "sec.gov": 1.0,
    "oracle.com": 0.8,
    "linkedin.com": 0.7,
    "indeed.com": 0.65,
    "myworkdayjobs.com": 0.65,
    "greenhouse.io": 0.7,
    "boards.greenhouse.io": 0.7,
    "lever.co": 0.7,
    "jobs.lever.co": 0.7,
}


def _now_utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return (
                datetime.strptime(s, fmt)
                .astimezone(timezone.utc)
                .isoformat(timespec="seconds")
            )
        except Exception:
            pass
    # loose yyyy-mm-dd extractor
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", s)
    if m:
        try:
            return (
                datetime.strptime(m.group(1), "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .isoformat(timespec="seconds")
            )
        except Exception:
            return None
    return None


def _hostname(u: str | None) -> str:
    if not u:
        return ""
    try:
        h = urlparse(u).netloc.lower()
        return h.split(":")[0]
    except Exception:
        return ""


def _category_for(hit: dict) -> str:
    text = " ".join(
        [
            hit.get("category", ""),
            hit.get("title", ""),
            hit.get("snippet", ""),
            hit.get("publisher", ""),
            hit.get("query", ""),
        ]
    ).lower()
    for cat, hints in CATEGORY_HINTS.items():
        for h in hints:
            if h in text:
                return cat

    host = _hostname(hit.get("url") or hit.get("link") or hit.get("id"))
    if any(k in host for k in ("investors.", "ir.")):
        return "Financial Filings & Capital Markets"
    if "careers" in host:
        return "Jobs & Hiring Signals"
    return "Market, News & Analyst Reports"


def _confidence_for(hit: dict) -> float:
    """Heuristic confidence score in [0.1, 1.0].

    Base is publisher/host-driven, with bumps for recency, trusted domains,
    and clear risk/issue language.
    """
    # Base from known publishers / hosts, otherwise default.
    host = _hostname(hit.get("url") or hit.get("link") or hit.get("id"))
    base = PUBLISHER_WEIGHTS.get(host, 0.6)

    # Recency: prefer explicit source/publish date, then timestamp/collected_at.
    date_str = (
        hit.get("source_date_iso")
        or hit.get("source_date")
        or hit.get("publish_date")
        or hit.get("date")
        or hit.get("timestamp")
        or hit.get("collected_at")
    )
    date_iso = _parse_date(date_str) if date_str else None
    if date_iso:
        try:
            dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days <= 30:
                base += 0.25
            elif age_days <= 180:
                base += 0.15
            elif age_days <= 540:
                base += 0.05
        except Exception:
            pass

    # Domain / host heuristics (IR, SEC, careers / jobs portals, etc.).
    if "sec.gov" in host:
        base = max(base, 0.95)
    if any(x in host for x in ("investors.", "ir.")):
        base = max(base, 0.9)
    if any(
        x in host
        for x in (
            "careers.",
            "/careers",
            "linkedin.com",
            "indeed.com",
            "myworkdayjobs.com",
            "greenhouse.io",
            "boards.greenhouse.io",
            "lever.co",
            "jobs.lever.co",
        )
    ):
        base += 0.05

    # Slight bump for obvious risk / issue language (issues pack and risk queries).
    text = " ".join(
        [
            hit.get("title", ""),
            hit.get("snippet", ""),
            hit.get("raw_excerpt", ""),
            hit.get("query", ""),
        ]
    ).lower()
    risk_terms = (
        "breach",
        "data breach",
        "ransomware",
        "security incident",
        "outage",
        "downtime",
        "lawsuit",
        "litigation",
        "class action",
        "investigation",
        "regulator",
        "enforcement",
        "fine",
        "sanction",
        "fraud",
        "whistleblower",
        "bankruptcy",
        "insolvent",
        "recall",
        "defect",
        "product failure",
        "quality issue",
        "shutdown",
        "explosion",
        "strike",
        "walkout",
        "boycott",
        "backlash",
        "scandal",
        "controversy",
    )
    if any(term in text for term in risk_terms):
        base += 0.1

    # Source-type bump: give a bit more weight to investor / IR / press style sources.
    src = (hit.get("source") or hit.get("publisher") or "").lower()
    if any(x in src for x in ("investor", "ir", "press", "newsroom")) or any(
        x in host for x in ("sec.gov", "investors.", "ir.")
    ):
        base += 0.1

    # Clamp and round.
    return max(0.1, min(1.0, round(base, 2)))


def _fingerprint(title: str, url: str, date_iso: str | None) -> str:
    key = f"{title.strip()}|{url.strip()}|{date_iso or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _normalize(hit: dict) -> dict | None:
    """Normalize a raw hit into a standard evidence record.

    Change from your earlier version:
    - We now consider `collected_at` as a fallback date source
      (date OR timestamp OR collected_at).
    - We also carry `collected_at` through on the record.
    """
    url = hit.get("url") or hit.get("link") or hit.get("id") or ""
    title = (hit.get("title") or hit.get("headline") or "").strip()
    if not url or not title:
        return None

    snippet = (hit.get("raw_excerpt") or hit.get("snippet") or "").strip()

    # Prefer explicit source/publish date, then fall back to timestamp/collected_at
    raw_source_date = (
        hit.get("source_date_iso")
        or hit.get("source_date")
        or hit.get("publish_date")
    )
    raw_date = raw_source_date or hit.get("date") or hit.get("timestamp") or hit.get("collected_at")
    date_iso = _parse_date(raw_date) if raw_date else None

    publisher = hit.get("publisher") or hit.get("source") or _hostname(url)
    category = _category_for(hit)
    confidence = _confidence_for(hit)

    return {
        "id": _fingerprint(title, url, date_iso),
        "title": title,
        "url": url,
        "date_iso": date_iso,
        "source_date_iso": _parse_date(raw_source_date) if raw_source_date else None,
        "snippet": snippet,
        "category": category,
        "publisher": publisher,
        "query": hit.get("query") or "",
        "source_type": hit.get("collected_via") or "web-search",
        "confidence": confidence,
        "collected_at": hit.get("collected_at"),
    }


# In-process callable used by run.py to avoid subprocess + disk I/O
# Accepts raw events as a Python list and returns the normalized object
# {"company": str, "generated_at": iso, "evidence": list}
def run_step(*, company: str, raw_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []

    for e in (raw_events or []):
        ne = _normalize(e)
        if not ne:
            continue
        fid = ne.get("id")
        if fid in seen:
            continue
        seen.add(fid)
        out.append(ne)

    # Sort: newest first, then confidence desc, then title
    def _sort_key(rec: dict):
        di = rec.get("date_iso")
        try:
            dt = datetime.fromisoformat((di or "").replace("Z", "+00:00"))
        except Exception:
            dt = datetime.fromtimestamp(0, tz=timezone.utc)
        return (
            -int(dt.timestamp()),
            -int(rec.get("confidence", 0) * 100),
            rec.get("title", "").lower(),
        )

    out.sort(key=_sort_key)
    return {"company": company, "generated_at": _now_utc_iso(), "evidence": out}


def harvest(
    raw_events_path: str,
    out_json: str,
    out_ndjson: str | None = None,
    company: str | None = None,
) -> dict:
    events = _read_events_any(raw_events_path)
    seen: set[str] = set()
    out: list[dict] = []

    for e in events:
        ne = _normalize(e)
        if not ne:
            continue
        if ne["id"] in seen:
            continue
        seen.add(ne["id"])
        out.append(ne)

    # Sort: newest first, then by confidence desc, then title
    def _sort_key(rec: dict):
        di = rec.get("date_iso")
        try:
            dt = datetime.fromisoformat((di or "").replace("Z", "+00:00"))
        except Exception:
            dt = datetime.fromtimestamp(0, tz=timezone.utc)
        return (
            -int(dt.timestamp()),
            -int(rec.get("confidence", 0) * 100),
            rec.get("title", "").lower(),
        )

    out.sort(key=_sort_key)

    out_obj = {
        "company": company,
        "generated_at": _now_utc_iso(),
        "evidence": out,
    }

    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).write_text(json.dumps(out_obj, indent=2), encoding="utf-8")

    if out_ndjson:
        with Path(out_ndjson).open("w", encoding="utf-8") as fh:
            for rec in out:
                fh.write(json.dumps(rec) + "\n")

    return out_obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument(
        "--raw-events",
        required=True,
        help="Path to Step-1 output (JSON or NDJSON).",
    )
    ap.add_argument(
        "--raw-events-ndjson",
        default="",
        help="Optional: NDJSON alternative to --raw-events.",
    )
    ap.add_argument(
        "--session-dir",
        default="",
        help="If provided, default output paths will be placed under this directory.",
    )
    ap.add_argument(
        "--out",
        default="",
        help="Path to write normalized evidence JSON (alias of --out-json).",
    )
    ap.add_argument(
        "--out-json",
        default="",
        help="Path to write normalized evidence JSON.",
    )
    ap.add_argument(
        "--out-ndjson",
        default="",
        help="Optional NDJSON path for flat records.",
    )
    args = ap.parse_args()

    out_json = args.out_json or args.out
    if not out_json:
        if args.session_dir:
            out_json = str(Path(args.session_dir) / "evidence.step2.json")
        else:
            raise SystemExit("Must provide --out/--out-json or --session-dir")

    raw_path = args.raw_events
    if args.raw_events_ndjson:
        raw_path = args.raw_events_ndjson

    # Read raw events and run in-process normalization
    raw_events = _read_events_any(raw_path)
    obj = run_step(company=args.company, raw_events=raw_events)

    # Write JSON artifact
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).write_text(json.dumps(obj, indent=2), encoding="utf-8")

    # Optional NDJSON of flat evidence records
    if args.out_ndjson:
        with Path(args.out_ndjson).open("w", encoding="utf-8") as fh:
            for rec in obj.get("evidence", []) or []:
                fh.write(json.dumps(rec) + "\n")

    # Human-readable completion log to stderr
    print(
        f"[step2] Completed. company={obj['company']} items={len(obj['evidence'])} -> {out_json}",
        file=sys.stderr,
    )
    if args.out_ndjson:
        print(
            f"[step2] Wrote NDJSON -> {args.out_ndjson}",
            file=sys.stderr,
        )

    # Machine-readable tails for run.py
    print(f"__STEP2_EVIDENCE_PATH__:{out_json}")
    print(f"__STEP2_EVIDENCE_COUNT__:{len(obj['evidence'])}")


if __name__ == "__main__":
    main()