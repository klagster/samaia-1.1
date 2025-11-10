#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 3: Hypotheses Generator (session-aware)
--------------------------------------------
Reads the normalized evidence index (from step2_evidence_harvester.py), clusters
signals, and generates evidenced business problems (hypotheses) that conform to
the SAMaiA "problems" schema used by run.py (Step 3 output shape).

Updates in this patch:
- **Session-aware**: accepts `--session-dir` and auto-discovers inputs/outputs.
  * Default evidence input: `<session-dir>/evidence.step2.json` (falls back to
    `<session-dir>/evidence.step2.ndjson` if JSON not found).
  * Default outputs: `<session-dir>/hypotheses.step3.json` and optional MD at
    `<session-dir>/hypotheses.step3.md` when `--emit-md` is set.
- **Orchestrator markers** printed to STDOUT for `run.py` to scrape:
  * `__STEP3_JSON_PATH__:<path>`
  * `__STEP3_MD_PATH__:<path>` (only if emitted)
  * `__STEP3_PROBLEM_COUNT__:<int>`
- **Backwards compatible** flags still work: `--evidence`, `--out-json`, `--out-md`.

Example (session-driven):
  python src/app/step3_hypotheses_generator.py \
    --session-dir .outputs/sessions/equinix_2025-11-04 \
    --company "Equinix" --time-window "last 12–18 months" --emit-md

Example (explicit paths):
  python src/app/step3_hypotheses_generator.py \
    --evidence .outputs/equinix_evidence_index.json \
    --company "Equinix" \
    --out-json .outputs/equinix_step3_hypotheses.json \
    --out-md .outputs/equinix_step3_hypotheses.md
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

# ----------------------------
# Utilities
# ----------------------------

ISO_NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_ndjson(path: str | Path) -> dict:
    """Load NDJSON records and coerce into evidence index shape.
    Expected each line to be a JSON object representing one evidence item.
    """
    items: List[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    items.append(obj)
            except Exception:
                continue
    return {"evidence": items}


def _is_http(url: str | None) -> bool:
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))


def _parse_date(d: str | None) -> Optional[datetime]:
    if not d or not isinstance(d, str):
        return None
    try:
        if len(d) >= 10 and d[4] == "-" and d[7] == "-":
            return datetime.fromisoformat(d[:10] + "T00:00:00+00:00")
        return datetime.fromisoformat(d)
    except Exception:
        return None


def _days_ago(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _domain(host: str | None) -> str:
    if not host:
        return "unknown"
    h = host.lower()
    h = re.sub(r"^(www|news|media|press|ir|investors|newsroom)\.", "", h)
    return h


def _extract_host(url: str | None) -> str:
    if not _is_http(url):
        return "unknown"
    try:
        return _domain(re.sub(r"^https?://", "", url).split("/")[0])
    except Exception:
        return "unknown"

# ----------------------------
# Heuristics / Taxonomy
# ----------------------------

CATEGORY_MAP = {
    "Financial Filings & Capital Markets": "Growth",
    "Corporate & Official Sources": "Operations",
    "Jobs & Hiring Signals": "Operations",
    "Risk, Compliance & Security": "Risk",
    "Technology & Operations (General)": "Operations",
    "Technology & Operations (Data Center / AI)": "Growth",
    "Sustainability & Energy": "Cost",
    "Market, News & Analyst Reports": "Growth",
    "Customer & Product": "CX",
    "M&A, Geography & Expansion": "Growth",
}

SOURCE_QUALITY_PRIOR = {
    # Official
    "sec.gov": 1.0, "europa.eu": 0.95, "ftc.gov": 0.95, "ico.org.uk": 0.95, "cnil.fr": 0.95,
    # Tier-1 media / wires
    "reuters.com": 0.95, "bloomberg.com": 0.95, "businesswire.com": 0.9, "prnewswire.com": 0.9, "globenewswire.com": 0.9,
    # Generic baseline
    "default": 0.7,
}

KPI_BY_PROB_CAT = {
    "Growth": ["Revenue growth", "Win rate", "Time-to-market", "CapEx efficiency"],
    "Cost": ["Operating margin", "Unit cost", "Energy cost per kWh", "PUE"],
    "Risk": ["# security incidents", "MTTD/MTTR", "Regulatory fines", "Audit pass rate"],
    "Operations": ["Change failure rate", "Deployment lead time", "SLA/SLO attainment", "Service availability"],
    "CX": ["NPS/CSAT", "Churn", "Time-to-value", "Support ticket volume"],
    "Talent": ["Time-to-hire", "Open reqs", "Attrition"],
}

TITLE_TEMPLATES = [
    (re.compile(r"\b(compliance|GDPR|DORA|NIS2|data\s+sovereignty)\b", re.I),
     "Need to Strengthen Global Security & Compliance Frameworks"),
    (re.compile(r"\b(breach|ransomware|exfiltration|incident|outage|vulnerability)\b", re.I),
     "Exposure to Cyber Risk Requires Hardening of Core Controls"),
    (re.compile(r"\b(acquisition|acquires|xScale|expansion|opens|greenfield|brownfield|region|facility)\b", re.I),
     "Aggressive Expansion Risks a Fragmented Operating Posture"),
    (re.compile(r"\b(credit facility|refinanc|notes offering|debt|financing)\b", re.I),
     "Capital Structure Changes Create Pressure for Efficient Growth"),
    (re.compile(r"\b(liquid cooling|immersion|H100|B200|GPU|high density|MW|substation|capacity)\b", re.I),
     "Scaling Infrastructure for High-Density AI/ML Workloads"),
    (re.compile(r"\b(pp[a]?|renewable|scope 1|scope 2|scope 3|emissions|SBTi|ESG)\b", re.I),
     "Balancing Sustainability Commitments with Operational Economics"),
    (re.compile(r"\b(vmware|private cloud|deployment|customer experience|onboarding)\b", re.I),
     "Simplifying Private Cloud Deployments to Improve CX"),
]


def _pick_title(snippet: str, default: str) -> str:
    for rx, title in TITLE_TEMPLATES:
        if rx.search(snippet or ""):
            return title
    return default


def _map_problem_category(ev_cat: str) -> str:
    return CATEGORY_MAP.get(ev_cat, "Operations")


def _urgency_from_days(days: Optional[int]) -> Tuple[str, str]:
    if days is None:
        return "Unknown", "Unknown"
    if days <= 90:
        return "High", "0–6m"
    if days <= 365:
        return "Medium", "6–18m"
    return "Low", "18m+"


def _score_confidence(source_q: float, days: Optional[int], count: int) -> float:
    recency = 1.0
    if days is not None:
        recency = max(0.2, 1.0 - (max(0, days - 90) / 900.0))
    multiplicity = min(1.0, 0.35 + math.log2(max(1, count)) / 4.0)
    raw = 0.35 * source_q + 0.40 * recency + 0.25 * multiplicity
    return round(max(0.05, min(0.98, raw)), 2)


def _source_quality_from_url(url: str) -> float:
    h = _extract_host(url)
    return SOURCE_QUALITY_PRIOR.get(h, SOURCE_QUALITY_PRIOR["default"])


def _collect_topic_terms(texts: List[str]) -> List[str]:
    bags = Counter()
    for t in texts:
        if not isinstance(t, str):
            continue
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", t.lower())
        for tok in tokens:
            if tok in {"the","and","for","with","into","from","that","this","have","has","are","was","were","will","can","not","but","you","our","their","about","over","under","between"}:
                continue
            bags[tok] += 1
    return [w for w,_ in bags.most_common(8)]

# ----------------------------
# Core
# ----------------------------


def build_hypotheses(evidence_index: dict, company: str, time_window: str, max_per_bucket: int = 3) -> dict:
    ev_items = evidence_index.get("evidence") or []
    by_cat: Dict[str, List[dict]] = defaultdict(list)
    for ev in ev_items:
        if not isinstance(ev, dict):
            continue
        url = ev.get("url") or ev.get("url_or_id") or ""
        if not _is_http(url):
            continue
        # Normalize fields we rely on downstream
        ev_norm = {
            "category": ev.get("category") or ev.get("cat") or "Uncategorized",
            "publisher": ev.get("publisher") or ev.get("source") or _extract_host(url),
            "url": url,
            "title": ev.get("title") or "",
            "snippet": ev.get("snippet") or ev.get("quote_or_note") or ev.get("raw_excerpt") or "",
            "date_iso": ev.get("date_iso") or ev.get("date") or "",
        }
        by_cat[ev_norm["category"]].append(ev_norm)

    problems: List[dict] = []

    for ev_cat in sorted(by_cat.keys()):
        bucket = by_cat[ev_cat]
        if not bucket:
            continue

        bucket.sort(key=lambda e: (
            _days_ago(_parse_date(e.get("date_iso") or "")) or 10**6,
            -_source_quality_from_url(e.get("url")),
        ))

        for i in range(0, min(len(bucket), max_per_bucket * 2), max_per_bucket):
            chunk = bucket[i:i + max_per_bucket]
            if not chunk:
                continue

            category_mapped = _map_problem_category(ev_cat)
            kpis = KPI_BY_PROB_CAT.get(category_mapped, [])[:4]

            top_date = _parse_date((chunk[0].get("date_iso") or ""))
            days = _days_ago(top_date)

            joined_snips = " ".join([(c.get("snippet") or c.get("title") or "") for c in chunk])
            default_title = f"Evidenced {category_mapped} Issue"
            title = _pick_title(joined_snips, default_title)

            evidence_arr = []
            texts_for_topics = []
            for c in chunk:
                url = c.get("url")
                if not _is_http(url):
                    continue
                quote = c.get("snippet") or c.get("title") or ""
                evidence_arr.append({
                    "source": c.get("publisher") or _extract_host(url),
                    "url_or_id": url,
                    "date": (c.get("date_iso") or "")[:10],
                    "quote_or_note": (quote or "")[:280],
                })
                texts_for_topics.append(quote)

            if not evidence_arr:
                continue

            avg_sq = sum(_source_quality_from_url(e["url_or_id"]) for e in evidence_arr) / max(1, len(evidence_arr))
            conf = _score_confidence(avg_sq, days, len(evidence_arr))

            urgency_level, time_hz = _urgency_from_days(days)
            impact = "High" if conf >= 0.7 else ("Medium" if conf >= 0.45 else "Low")

            top_terms = _collect_topic_terms(texts_for_topics)[:5]
            why = (
                f"Observed signals point to {category_mapped.lower()} pressure around: "
                + ", ".join(top_terms) if top_terms else
                f"Observed signals indicate {category_mapped.lower()} considerations."
            )

            blind_spots = [
                "Exact scope and budget of the initiative are not stated.",
                "Internal success metrics and timelines are not disclosed.",
            ]
            validations = [
                "Confirm scope (regions, products, or assets) implicated by these signals.",
                "Request KPIs and milestones tied to this initiative.",
            ]

            problems.append({
                "title": title,
                "category": category_mapped,
                "why_it_matters": why,
                "primary_stakeholder": "Unknown",
                "secondary_stakeholders": [],
                "evidence": evidence_arr,
                "artifact_refs": [],
                "affected_kpis": kpis,
                "competitive_context": "Unknown",
                "urgency": {"level": urgency_level, "time_horizon": time_hz},
                "impact": impact,
                "confidence": conf,
                "blind_spots": blind_spots,
                "validation_questions": validations,
                "value_prop_mapping": "Unknown",
            })

    return {
        "company": company,
        "generated_at": ISO_NOW,
        "time_window": time_window,
        "problems": problems,
    }


def render_markdown(payload: dict) -> str:
    lines = []
    lines.append(f"# Step 3: Evidenced Hypotheses for {payload.get('company','Unknown')}")
    lines.append(f"_Generated at: {payload.get('generated_at','')}; Time window: {payload.get('time_window','')}_\n")
    problems = payload.get("problems") or []
    if not problems:
        lines.append("> No evidenced hypotheses generated from the provided evidence index.")
        return "\n".join(lines)

    for i, p in enumerate(problems, 1):
        lines.append(f"## {i}. {p.get('title','Untitled')}  \n")
        lines.append(
            f"**Category:** {p.get('category','Unknown')}  \n"
            f"**Impact:** {p.get('impact','Unknown')}  \n"
            f"**Urgency:** {p.get('urgency',{}).get('level','Unknown')} "
            f"({p.get('urgency',{}).get('time_horizon','Unknown')})  \n"
            f"**Confidence:** {p.get('confidence',0):.2f}\n"
        )
        lines.append(f"**Why it matters:** {p.get('why_it_matters','')}\n")
        kpis = p.get("affected_kpis") or []
        if kpis:
            lines.append("**Affected KPIs:** " + ", ".join(kpis) + "\n")
        lines.append("**Sources & Citations:**")
        evs = p.get("evidence") or []
        for ev in evs:
            nm = ev.get("source", "Source")
            url = ev.get("url_or_id", "")
            dt = ev.get("date", "")
            qt = (ev.get("quote_or_note", "").strip())
            qt = (qt[:180] + "…") if len(qt) > 180 else qt
            if _is_http(url):
                lines.append(f"- [{nm}]({url}) — {qt} ({dt})")
            else:
                lines.append(f"- {nm}: {qt} ({dt})")
        lines.append("")
    return "\n".join(lines)


def _discover_inputs_outputs(args: argparse.Namespace) -> tuple[Path, Path, Optional[Path]]:
    """Determine evidence input and output paths based on CLI flags/session-dir."""
    session_dir = Path(args.session_dir) if args.session_dir else None

    # Input resolution order: --evidence, session JSON, session NDJSON
    if args.evidence:
        evidence_path = Path(args.evidence)
        if not evidence_path.exists():
            raise FileNotFoundError(f"Evidence not found: {evidence_path}")
    elif session_dir:
        cand_json = session_dir / "evidence.step2.json"
        cand_ndj = session_dir / "evidence.step2.ndjson"
        if cand_json.exists():
            evidence_path = cand_json
        elif cand_ndj.exists():
            evidence_path = cand_ndj
        else:
            raise FileNotFoundError(
                f"No evidence file found in session dir: {session_dir} (looked for evidence.step2.json / evidence.step2.ndjson)"
            )
    else:
        raise ValueError("Provide --evidence or --session-dir for input discovery.")

    # Output JSON
    if args.out_json:
        out_json = Path(args.out_json)
    elif session_dir:
        out_json = session_dir / "hypotheses.step3.json"
    else:
        raise ValueError("Provide --out-json or --session-dir to determine output path.")

    # Output MD (optional): from --out-md or --emit-md flag
    out_md: Optional[Path] = None
    if args.out_md:
        out_md = Path(args.out_md)
    elif session_dir and args.emit_md:
        out_md = session_dir / "hypotheses.step3.md"

    return evidence_path, out_json, out_md


def main():
    ap = argparse.ArgumentParser(description="Step 3 Hypotheses Generator (deterministic, evidence-first, session-aware)")
    ap.add_argument("--evidence", required=False, help="Path to evidence_index.json (or NDJSON) from Step 2 harvester")
    ap.add_argument("--company", required=False, default=None, help="Company name (overrides evidence header)")
    ap.add_argument("--time-window", required=False, default="last 12–18 months")
    ap.add_argument("--out-json", required=False, help="Output JSON path for hypotheses")
    ap.add_argument("--out-md", required=False, help="Optional Markdown summary path")
    ap.add_argument("--max-per-bucket", type=int, default=3, help="Max evidence items per hypothesis")
    ap.add_argument("--session-dir", required=False, help="Session directory to discover inputs/outputs and write artifacts")
    ap.add_argument("--emit-md", action="store_true", help="When using --session-dir, also emit a Markdown summary next to JSON")
    args = ap.parse_args()

    # Discover paths based on args/session
    try:
        evidence_path, out_json, out_md = _discover_inputs_outputs(args)
    except Exception as e:
        print(f"[Step 3] Input/Output resolution error: {e}", file=sys.stderr)
        sys.exit(2)

    # Load evidence (JSON or NDJSON)
    try:
        if evidence_path.suffix.lower() in {".ndjson", ".ndj"}:
            evidence_index = _load_ndjson(evidence_path)
        else:
            evidence_index = _load_json(evidence_path)
    except Exception as e:
        print(f"[Step 3] Failed to load evidence: {e}", file=sys.stderr)
        sys.exit(2)

    company = args.company or evidence_index.get("company") or "Unknown"

    payload = build_hypotheses(
        evidence_index=evidence_index,
        company=company,
        time_window=args.time_window,
        max_per_bucket=max(1, args.max_per_bucket),
    )

    # Write JSON
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"[Step 3] Wrote hypotheses JSON → {out_json}")

    # Optional Markdown
    if out_md:
        md = render_markdown(payload)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        with open(out_md, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"[Step 3] Wrote Markdown summary → {out_md}")

    # Emit orchestrator markers for run.py
    print(f"__STEP3_JSON_PATH__:{out_json}")
    if out_md:
        print(f"__STEP3_MD_PATH__:{out_md}")
    print(f"__STEP3_PROBLEM_COUNT__:{len(payload.get('problems') or [])}")


if __name__ == "__main__":
    main()