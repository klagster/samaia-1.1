#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 5 — Compelling Events (deterministic, no model calls)

Inputs:
  --issues        Path to Step 2 JSON (problems) OR Step 3 JSON (optional; if given, we’ll map by title)
                  - Step 2 schema: {"company","generated_at","time_window","problems":[{...}]}
  --alignments    Path to Step 3 JSON (issue–challenge alignments, optional but recommended)
                  - Step 3 schema: {"company","time_window","alignments":[{...}]}
  --evidence      Optional: extra harvested evidence (NDJSON or JSON array of dicts)
                  Accepts keys: source/name, url/url_or_id, date/observed_at/timestamp, quote_or_note/snippet/raw_excerpt
  --company       Optional: override company name
  --out-json      REQUIRED: output JSON path
  --out-md        Optional: write an exec-ready Markdown summary
  --max-sources   Max sources per event (default 3)
  --strict        Evidence strictness: high|medium|low (default medium)

Behavior:
  • One compelling event per aligned issue (Step 3). If no Step 3, we derive events from Step 2 problems.
  • Carries forward URLs/snippets; never fabricates links.
  • Confidence & urgency are scored from evidence volume, recency, and alignment strength.
  • Markdown includes “Sources & Citations” bullets.

This script does not import or require run.py; it can be called independently,
and its JSON output can be picked up by your wrappers.
"""

from __future__ import annotations
import argparse, json, os, re
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Iterable
from datetime import datetime, timezone
from typing import Tuple
# ---------------------- Utilities ----------------------

def first_existing(*candidates: Path) -> Optional[Path]:
    for c in candidates:
        if c and c.exists():
            return c
    return None

def make_abs(p: Optional[Path]) -> Optional[Path]:
    return p.resolve() if isinstance(p, Path) else None

# ---------------------- Utilities ----------------------

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

def parse_date_safe(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for f in DATE_FORMATS:
        try:
            dt = datetime.strptime(s, f)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    # loose fallbacks
    try:
        if re.fullmatch(r"\d{4}-\d{2}", s):
            return datetime.strptime(s+"-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if re.fullmatch(r"\d{4}", s):
            return datetime.strptime(s+"-01-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None

def is_http(url: Optional[str]) -> bool:
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def read_json_or_ndjson_list(path: Optional[Path]) -> List[dict]:
    if not path or not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    # try JSON first
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass
    # ndjson fallback
    out: List[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    return out

def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb: return 0.0
    return len(sa & sb) / len(sa | sb)

def toks(s: str) -> List[str]:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return [w for w in s.split() if w]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------------------- Data classes ----------------------

@dataclass
class EvidenceItem:
    source: str
    url: str
    date: str
    quote: str

    @staticmethod
    def from_any(d: dict) -> Optional["EvidenceItem"]:
        if not isinstance(d, dict): return None
        url = d.get("url") or d.get("url_or_id") or d.get("id") or ""
        if not is_http(url):
            return None
        # prefer date-like fields
        dt = d.get("date") or d.get("observed_at") or d.get("timestamp") or ""
        dt = str(dt)[:10] if dt else ""
        src = d.get("source") or d.get("name") or "Web"
        quote = d.get("quote_or_note") or d.get("snippet") or d.get("raw_excerpt") or d.get("title") or ""
        if isinstance(quote, str) and len(quote) > 400:
            quote = quote[:397] + "..."
        return EvidenceItem(source=src, url=url, date=dt, quote=quote)

# ---------------------- Heuristics ----------------------

STAKEHOLDER_DEFAULT = {
    "Growth": "CRO / GM",
    "Cost": "CFO",
    "Risk": "CISO / Chief Risk Officer",
    "Operations": "VP, Operations",
    "CX": "VP, Customer Experience",
    "Talent": "CHRO",
}

def score_confidence(num_sources: int, newest_days: Optional[int], alignment_strength: Optional[str]) -> float:
    base = 0.2 + min(num_sources, 5) * 0.12   # up to ~0.8
    if newest_days is not None:
        if newest_days <= 60: base += 0.1
        elif newest_days <= 180: base += 0.05
    if alignment_strength:
        m = {"Strong": 0.1, "Partial": 0.03, "Weak": 0.0}
        base += m.get(alignment_strength, 0.0)
    return max(0.05, min(base, 0.95))

def derive_urgency(newest_days: Optional[int]) -> Dict[str, str]:
    if newest_days is None:
        return {"level": "Unknown", "time_horizon": "Unknown"}
    if newest_days <= 90:
        return {"level": "High", "time_horizon": "0–6m"}
    if newest_days <= 180:
        return {"level": "Medium", "time_horizon": "6–18m"}
    return {"level": "Low", "time_horizon": "18m+"}

# ---------------------- Loaders ----------------------

def load_step2_problems(p: Optional[Path]) -> List[dict]:
    if not p or not p.exists(): return []
    obj = read_json(p)
    if isinstance(obj, dict) and isinstance(obj.get("problems"), list):
        return [x for x in obj["problems"] if isinstance(x, dict)]
    return []

def load_step3_alignments(p: Optional[Path]) -> List[dict]:
    if not p or not p.exists(): return []
    obj = read_json(p)
    if isinstance(obj, dict) and isinstance(obj.get("alignments"), list):
        return [x for x in obj["alignments"] if isinstance(x, dict)]
    return []

def collect_problem_evidence(prob: dict) -> List[EvidenceItem]:
    out: List[EvidenceItem] = []
    for ev in (prob.get("evidence") or []):
        item = EvidenceItem.from_any(ev)
        if item: out.append(item)
    return out

# ---------------------- Core synthesis ----------------------

def build_event_for_issue(
    issue_title: str,
    issue_category: str,
    aligned: List[dict],
    pooled_evidence: List[EvidenceItem],
    strict: str,
    max_sources: int,
) -> dict:
    # pick stakeholder by category
    stakeholder = STAKEHOLDER_DEFAULT.get(issue_category or "", "Executive Sponsor")

    # pick 1–3 challenges
    aligned_ch_names = []
    align_strength = None
    if aligned:
        # prefer up to 3 strongest (Strong > Partial > Weak)
        def strength_rank(a: dict) -> int:
            s = (a.get("alignment_strength") or "").strip()
            return {"Strong": 0, "Partial": 1, "Weak": 2}.get(s, 3)
        chosen = sorted(aligned, key=strength_rank)[:3]
        aligned_ch_names = [c.get("challenge") for c in chosen if c.get("challenge")]
        if chosen:
            align_strength = chosen[0].get("alignment_strength")

    # evidence selection
    # keep up to N, preferring newest
    ev_sorted = sorted(
        pooled_evidence,
        key=lambda e: (parse_date_safe(e.date) or datetime(1970,1,1, tzinfo=timezone.utc)),
        reverse=True,
    )
    chosen_ev = ev_sorted[:max_sources]

    # newest age
    newest_days = None
    if chosen_ev:
        newest_dt = parse_date_safe(chosen_ev[0].date)
        if newest_dt:
            newest_days = int((datetime.now(timezone.utc) - newest_dt).total_seconds() // 86400)

    confidence = score_confidence(len(chosen_ev), newest_days, align_strength)
    urgency = derive_urgency(newest_days)

    # Messages (concise, exec)
    if issue_category == "Risk":
        event_message = (
            f"As {issue_title} persists, gaps increase the probability of fines, outages, or data loss. "
            "Standardizing controls and visibility now reduces audit friction and breach exposure."
        )
        risk_if_ignored = "Escalating likelihood of non-compliance, regulatory penalties, and incident-driven churn."
        opportunity = "A unified control plane that simplifies compliance, shortens audits, and hardens defenses globally."
    elif issue_category == "Growth":
        event_message = (
            f"{issue_title} is constraining scale velocity. Streamlining architecture unlocks faster market entry and higher win rates."
        )
        risk_if_ignored = "Integration delays, missed revenue windows, and competitive displacement."
        opportunity = "Accelerated time-to-market via standard patterns and automation across regions."
    elif issue_category == "Cost":
        event_message = (
            f"{issue_title} is inflating OpEx/CapEx. Addressing it now captures efficiency gains this fiscal year."
        )
        risk_if_ignored = "Budget overruns and margin erosion due to duplicated tools and manual work."
        opportunity = "Lower run-rate via consolidation, policy reuse, and automation."
    else:
        event_message = f"{issue_title} is a priority; resolving it creates measurable business benefit this cycle."
        risk_if_ignored = "Drift, rework, and value leakage."
        opportunity = "Crisper execution and measurable outcomes tied to KPIs."

    # Sources block
    sources = [
        {"name": ev.source, "url": ev.url, "date": ev.date, "quote_or_note": ev.quote}
        for ev in chosen_ev
    ]

    return {
        "issue_title": issue_title,
        "aligned_challenges": aligned_ch_names,
        "event_message": event_message,
        "stakeholder": stakeholder,
        "urgency_trigger": (
            "Recent signals and alignment indicate an active, funded initiative requiring standardization now."
            if newest_days is not None else
            "Active initiative signaled; timing window likely this planning cycle."
        ),
        "risk_if_ignored": risk_if_ignored,
        "opportunity_if_addressed": opportunity,
        "confidence": round(confidence, 2),
        "sources": sources
    }

def synthesize_events(
    step2_problems: List[dict],
    step3_alignments: List[dict],
    extra_ev: List[dict],
    strict: str,
    max_sources: int,
) -> Dict[str, Any]:
    # Build a lookup from problem title -> problem (for evidence)
    prob_by_title = { (p.get("title") or "").strip(): p for p in step2_problems if isinstance(p, dict) }

    # Pool extra evidence
    pooled_extra: List[EvidenceItem] = []
    for d in extra_ev or []:
        item = EvidenceItem.from_any(d)
        if item: pooled_extra.append(item)

    events_out: List[dict] = []

    if step3_alignments:
        for a in step3_alignments:
            title = (a.get("issue_title") or "").strip()
            category = (a.get("issue_category") or "Unknown").strip()
            aligned = a.get("aligned_challenges") or []

            # gather evidence from the matching Step 2 problem
            prob = prob_by_title.get(title)
            ev_from_prob = collect_problem_evidence(prob) if prob else []

            # combine with extra evidence
            pooled = ev_from_prob + pooled_extra

            # If strict=high and pooled is empty → still emit but sources=[]
            event = build_event_for_issue(
                issue_title=title or "Unspecified Issue",
                issue_category=category or "Unknown",
                aligned=aligned if isinstance(aligned, list) else [],
                pooled_evidence=pooled,
                strict=strict,
                max_sources=max_sources,
            )
            events_out.append(event)
    else:
        # No Step 3 given — synthesize events directly from Step 2 problems
        for p in step2_problems:
            title = (p.get("title") or "Unspecified Issue").strip()
            category = (p.get("category") or "Unknown").strip()
            ev = collect_problem_evidence(p) + pooled_extra
            event = build_event_for_issue(
                issue_title=title,
                issue_category=category,
                aligned=[],
                pooled_evidence=ev,
                strict=strict,
                max_sources=max_sources,
            )
            events_out.append(event)

    return {
        "generated_at": now_iso(),
        "compelling_events": events_out
    }


# ---------------------- In-process callable for orchestrator ----------------------

def run_step(
    *,
    problems: List[dict] | None,
    alignments: List[dict] | None,
    extra_evidence: List[dict] | None,
    strict: str = "medium",
    max_sources: int = 3,
    company: Optional[str] = None,
) -> Dict[str, Any]:
    """Run Step 5 fully in-process.

    Parameters mirror the synthesized inputs produced by prior steps when called
    from run.py. This avoids subprocess + disk I/O while preserving output shape.
    """
    # Normalize extra evidence into a uniform dict shape (same as CLI path)
    extra_norm: List[dict] = []
    for d in (extra_evidence or []):
        item = EvidenceItem.from_any(d)
        if item:
            extra_norm.append({
                "source": item.source,
                "url_or_id": item.url,
                "date": item.date,
                "quote_or_note": item.quote,
            })

    payload = synthesize_events(
        step2_problems=problems or [],
        step3_alignments=alignments or [],
        extra_ev=extra_norm,
        strict=strict,
        max_sources=max_sources,
    )

    if company:
        payload = {"company": company, **payload}

    return payload

# ---------------------- Markdown rendering ----------------------

def render_markdown(company: str, payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Compelling Events — {company}")
    lines.append(f"_Generated at: {payload.get('generated_at')}_")
    lines.append("")
    for ev in payload.get("compelling_events", []):
        lines.append(f"## {ev.get('issue_title','(untitled)')}")
        if ev.get("aligned_challenges"):
            lines.append("**Aligned challenges:** " + ", ".join(ev["aligned_challenges"]))
        lines.append("")
        lines.append(ev.get("event_message","").strip())
        lines.append("")
        lines.append(f"- **Stakeholder:** {ev.get('stakeholder','')}")
        u = ev.get("urgency_trigger") or ""
        lines.append(f"- **Urgency trigger:** {u}")
        lines.append(f"- **Risk if ignored:** {ev.get('risk_if_ignored','')}")
        lines.append(f"- **Opportunity if addressed:** {ev.get('opportunity_if_addressed','')}")
        lines.append(f"- **Confidence:** {ev.get('confidence',0):.2f}")
        lines.append("")
        srcs = ev.get("sources") or []
        if srcs:
            lines.append("**Sources & Citations**")
            for s in srcs:
                name = s.get("name") or "Source"
                url = s.get("url") or ""
                date = s.get("date") or ""
                note = s.get("quote_or_note") or ""
                if is_http(url):
                    lines.append(f"- [{name}]({url}) — {note} ({date})")
            lines.append("")
        else:
            lines.append("**Sources & Citations:** None (no verifiable URLs in inputs)\n")
    return "\n".join(lines)

# ---------------------- CLI ----------------------

def main():
    ap = argparse.ArgumentParser(description="Step 5 — Compelling Events")
    ap.add_argument("--issues", type=str, help="Path to Step 2 JSON (problems) or Step 3 JSON if problems embedded")
    ap.add_argument("--alignments", type=str, help="Path to Step 3 JSON (alignments)", default=None)
    ap.add_argument("--evidence", type=str, help="Optional extra evidence JSON/NDJSON", default=None)
    ap.add_argument("--company", type=str, help="Override company name", default=None)
    ap.add_argument("--out-json", type=str, required=False, help="Output JSON path")
    ap.add_argument("--out-md", type=str, help="Optional Markdown output path", default=None)
    ap.add_argument("--max-sources", type=int, default=int(os.getenv("STEP5_MAX_SOURCES", "3")))
    ap.add_argument("--strict", choices=["high","medium","low"], default=os.getenv("STEP5_STRICT","medium"))
    ap.add_argument("--session-dir", type=str, help="Session directory for orchestrated runs", default=None)
    ap.add_argument("--emit-md", action="store_true", help="If set, write Markdown alongside JSON (session default path if out-md not provided)")
    args = ap.parse_args()

    session_dir = Path(args.session_dir).resolve() if args.session_dir else None

    # Resolve inputs/outputs using session defaults if session_dir is provided
    issues_path = Path(args.issues) if args.issues else None
    aligns_path = Path(args.alignments) if args.alignments else None
    ev_path = Path(args.evidence) if args.evidence else None
    out_json = Path(args.out_json) if args.out_json else None
    out_md = Path(args.out_md) if args.out_md else None

    if session_dir:
        if not issues_path:
            issues_path = session_dir / "problems.step2.json"
        if not aligns_path:
            aligns_path = session_dir / "alignments.step4.json"
        if not ev_path:
            ev_path = first_existing(
                session_dir / "evidence.step1.5.ndjson",
                session_dir / "evidence.step1.5.json",
                session_dir / "web_evidence.step1.ndjson",
                session_dir / "web_evidence.step1.json",
            )
        if not out_json:
            out_json = session_dir / "compelling_events.step5.json"
        if (args.emit_md or out_md) and not out_md:
            out_md = session_dir / "compelling_events.step5.md"

    if not out_json:
        ap.error("--out-json is required unless --session-dir is provided (which supplies a default path).")

    # Load inputs
    step2_problems = load_step2_problems(issues_path)
    step3_alignments = load_step3_alignments(aligns_path)
    extra_ev_raw = read_json_or_ndjson_list(ev_path)

    # Company inference
    company = args.company or "Unknown Company"
    # Try to infer from Step 2 or Step 3
    try:
        if issues_path and issues_path.exists():
            o = read_json(issues_path)
            company = o.get("company", company)
        if aligns_path and aligns_path.exists():
            o = read_json(aligns_path)
            company = o.get("company", company)
    except Exception:
        pass

    # Normalize extra evidence
    extra_evidence: List[dict] = []
    for d in extra_ev_raw:
        item = EvidenceItem.from_any(d)
        if item:
            extra_evidence.append({
                "source": item.source, "url_or_id": item.url, "date": item.date, "quote_or_note": item.quote
            })

    # Synthesize
    payload = synthesize_events(
        step2_problems=step2_problems,
        step3_alignments=step3_alignments,
        extra_ev=extra_evidence,
        strict=args.strict,
        max_sources=args.max_sources,
    )
    # Attach company
    payload = {"company": company, **payload}

    # Write JSON
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # Optional MD
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_markdown(company, payload), encoding="utf-8")

    print(f"[step5] Wrote JSON: {out_json}")
    if out_md:
        print(f"[step5] Wrote Markdown: {out_md}")

    # Orchestrator markers
    abs_json = make_abs(out_json)
    if abs_json:
        print(f"__STEP5_JSON_PATH__:{abs_json}")
    if out_md:
        abs_md = make_abs(out_md)
        if abs_md:
            print(f"__STEP5_MD_PATH__:{abs_md}")
    try:
        count = len(payload.get("compelling_events", []))
    except Exception:
        count = 0
    print(f"__STEP5_EVENT_COUNT__:{count}")

if __name__ == "__main__":
    main()