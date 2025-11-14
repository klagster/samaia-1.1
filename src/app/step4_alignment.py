#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 4: Issue–Challenge Alignment (deterministic, no model calls)
-----------------------------------------------------------------
Inputs:
  --issues   /path/to/step3_hypotheses.json   (output of step3_hypotheses_generator.py)
  --taxonomy /path/to/campaign_challenges.json
  --company "Equinix"                         (optional override; else taken from issues JSON)
  --time-window "last 12–18 months"           (optional override)
  --out-json /tmp/alignments.json             (required; Step-C JSON for pipeline)
  --out-md   /tmp/alignments.md               (optional pretty summary)
  --top-k 3                                   (number of aligned challenges per issue, default 3)

What this script does:
- Flattens an arbitrary taxonomy into [(group, challenge_text)].
- Builds a term-weighted representation of each issue (title + why_it_matters + evidence quotes).
- Computes similarity vs. each challenge using a hybrid score:
    * Token overlap (Jaccard of unigrams+bigrams)
    * Soft keyword match boost (domain/KPI/verb hints)
- Selects top-K matches with a rationale and an alignment_strength bucket.
- If no good matches, leaves aligned_challenges empty and populates 'gaps'.
- Produces JSON that matches the Step-C schema expected by run.py: build_step_c_prompt().

No external services; fully deterministic and side-effect free.

Session-aware usage
-------------------
Instead of passing explicit paths, you can provide:

  --session-dir /path/to/session_dir
  [--emit-md]                      # optional; if present, also writes a Markdown summary

When --session-dir is provided:
  Inputs (defaults):
    <session-dir>/hypotheses.step3.json
    <session-dir>/campaign_challenges.json        # taxonomy
  Outputs (defaults):
    <session-dir>/alignments.step4.json
    <session-dir>/alignments.step4.md             # only if --emit-md or --out-md is set

The script also prints orchestrator markers for run.py to consume:
  __STEP4_JSON_PATH__:/abs/path/to/alignments.step4.json
  __STEP4_MD_PATH__:/abs/path/to/alignments.step4.md          # only if emitted
  __STEP4_ALIGN_COUNT__:<int>
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional, Iterable

ISO_NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

def _require_file(p: Path, what: str) -> Path:
    if not p.exists():
        raise FileNotFoundError(f"{what} not found: {p}")
    return p

def _default_session_inputs(session_dir: Path) -> tuple[Path, Path]:
    issues = session_dir / "hypotheses.step3.json"
    taxonomy = session_dir / "campaign_challenges.json"
    return issues, taxonomy

def _default_session_outputs(session_dir: Path, emit_md: bool) -> tuple[Path, Optional[Path]]:
    out_json = session_dir / "alignments.step4.json"
    out_md = (session_dir / "alignments.step4.md") if emit_md else None
    return out_json, out_md

# ----------------------------
# Basic NLP helpers
# ----------------------------

STOP = {
    "the","and","for","with","into","from","that","this","have","has","had","are","was","were","will","can","could",
    "not","but","you","our","their","about","over","under","between","to","of","in","on","at","by","as","it","is","be",
    "or","if","an","a","we","they","them","these","those","your","its","than","then","also","more","most","less","least",
    "across","per","via","vs","&","—","–"
}

DOMAIN_HINTS = {
    # Risk/Compliance
    "risk": 1.1, "breach": 1.2, "ransomware": 1.3, "incident": 1.1, "exfiltration": 1.2, "audit": 1.2,
    "gdpr": 1.3, "dora": 1.25, "nis2": 1.25, "sox": 1.15, "soc2": 1.2, "iso27001": 1.2, "compliance": 1.2,
    "policy": 1.05, "governance": 1.1, "privacy": 1.15, "data": 1.05, "sovereignty": 1.2,

    # Growth / Infra / AI
    "gpu": 1.25, "nvidia": 1.15, "h100": 1.25, "b200": 1.25, "ai": 1.1, "ml": 1.1, "density": 1.15, "cooling": 1.15,
    "liquid": 1.15, "immersion": 1.1, "capacity": 1.1, "mw": 1.1, "substation": 1.05, "colocation": 1.05,

    # Cost / Sustainability
    "pue": 1.25, "efficiency": 1.15, "renewable": 1.15, "ppa": 1.15, "emissions": 1.1, "sbti": 1.1, "scope": 1.05,
    "energy": 1.15, "power": 1.1, "carbon": 1.1, "cost": 1.05,

    # Ops / CX
    "onboarding": 1.1, "deployment": 1.1, "customer": 1.05, "experience": 1.05, "cx": 1.1, "sla": 1.1, "slo": 1.1,
    "availability": 1.05, "latency": 1.05, "reliability": 1.05,

    # Finance / Capital markets
    "capex": 1.15, "opex": 1.15, "credit": 1.1, "facility": 1.05, "refinanc": 1.15, "notes": 1.05, "debt": 1.1
}

def _tokens(text: str) -> List[str]:
    if not isinstance(text, str):
        return []
    # normalize hyphens/emdash to space
    t = text.lower().replace("—", " ").replace("–", " ")
    t = re.sub(r"[^a-z0-9\- ]+", " ", t)
    toks = [w for w in t.split() if w and w not in STOP]
    return toks

def _bigrams(toks: List[str]) -> List[str]:
    return [f"{toks[i]}_{toks[i+1]}" for i in range(len(toks)-1)] if len(toks) >= 2 else []

def _feat(texts: Iterable[str]) -> Counter:
    bag = Counter()
    for txt in texts:
        toks = _tokens(txt)
        for w in toks:
            bag[w] += 1
            # domain-specific boosts
            for hint, mult in DOMAIN_HINTS.items():
                if w.startswith(hint):
                    bag[w] = int(math.ceil(bag[w] * mult))
        for bi in _bigrams(toks):
            bag[bi] += 1
    return bag

def _jaccard(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    set_a = set(a.keys())
    set_b = set(b.keys())
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return inter / union

def _soft_dot(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    # weighted overlap (acts like a crude TF dot)
    keys = set(a.keys()) & set(b.keys())
    return float(sum(min(a[k], b[k]) for k in keys))

def _hybrid_sim(a: Counter, b: Counter) -> float:
    # Combine Jaccard (structure) with weighted overlap (intensity)
    return 0.55 * _jaccard(a, b) + 0.45 * _soft_dot(a, b) / (1.0 + len(b))

# ----------------------------
# Taxonomy parsing / flattening
# ----------------------------

def _flatten_taxonomy(blob: Any) -> List[Tuple[str, str]]:
    """
    Return list of (group_name, challenge_text).
    Accepts several shapes:
      { "challenges": ["...", "..."] }
      { "groups": [{"name": "...","challenges":["..."]}, ...] }
      { "compliance_and_security": ["Ensuring Consistent Security Posture", ...], ... }
      or any dict[str, list[str]]
    """
    out: List[Tuple[str, str]] = []

    if isinstance(blob, dict):
        # Direct list under key "challenges"
        if isinstance(blob.get("challenges"), list):
            for c in blob["challenges"]:
                if isinstance(c, str) and c.strip():
                    out.append(("general", c.strip()))

        # Named groups
        if isinstance(blob.get("groups"), list):
            for g in blob["groups"]:
                gname = (g.get("name") or "group").strip() if isinstance(g, dict) else "group"
                challs = g.get("challenges") if isinstance(g, dict) else None
                if isinstance(challs, list):
                    for c in challs:
                        if isinstance(c, str) and c.strip():
                            out.append((gname, c.strip()))

        # Generic dict-of-lists
        for k, v in blob.items():
            if k in {"challenges", "groups"}:
                continue
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                for c in v:
                    cs = c.strip()
                    if cs:
                        out.append((k, cs))

    elif isinstance(blob, list):
        # List of strings => general group
        if all(isinstance(x, str) for x in blob):
            for c in blob:
                cs = c.strip()
                if cs:
                    out.append(("general", cs))
        # List of dicts with "challenge"
        for item in blob:
            if isinstance(item, dict) and isinstance(item.get("challenge"), str):
                g = item.get("group") or item.get("challenge_group") or "group"
                out.append((str(g), item["challenge"].strip()))

    # Deduplicate (group, challenge)
    seen = set()
    dedup: List[Tuple[str, str]] = []
    for g, c in out:
        key = (g.lower().strip(), c.lower().strip())
        if key in seen:
            continue
        seen.add(key)
        dedup.append((g, c))
    return dedup

# ----------------------------
# Alignment strength bucketing
# ----------------------------

def _strength(sim: float) -> str:
    if sim >= 0.30:
        return "Strong"
    if sim >= 0.15:
        return "Partial"
    return "Weak"

def _short_rationale(issue_title: str, challenge: str, sim: float) -> str:
    if sim == 0.0:
        return "No clear lexical overlap; potential conceptual gap."
    # Keep it concise
    return f"Overlap on terms/themes between '{issue_title}' and '{challenge}'. Similarity={sim:.2f}"

# ----------------------------
# Core alignment
# ----------------------------

def _issue_features(p: dict) -> Counter:
    parts = [
        p.get("title",""),
        p.get("why_it_matters",""),
    ]
    for ev in p.get("evidence") or []:
        q = ev.get("quote_or_note") or ""
        parts.append(q)
    return _feat(parts)

def align_issues_to_taxonomy(
    issues_json: dict,
    taxonomy_json: dict,
    top_k: int = 3
) -> Dict[str, Any]:
    company = issues_json.get("company") or "Unknown"
    time_window = issues_json.get("time_window") or "Unknown"
    problems = issues_json.get("problems") or []

    # Flatten taxonomy
    flat = _flatten_taxonomy(taxonomy_json)
    # Build features for challenges
    chall_vecs = [(grp, ch, _feat([ch])) for (grp, ch) in flat]

    alignments: List[Dict[str, Any]] = []

    for p in problems:
        title = p.get("title") or "Untitled"
        category = p.get("category") or "Operations"

        v_issue = _issue_features(p)
        scored: List[Tuple[float, str, str]] = []  # (sim, grp, challenge)
        for grp, ch, v_ch in chall_vecs:
            sim = _hybrid_sim(v_issue, v_ch)
            scored.append((sim, grp, ch))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Pick top-K above a tiny floor; if nothing clears 0.05, return empty
        picks = [s for s in scored[:max(1, top_k)] if s[0] >= 0.05]

        aligned_list = []
        for sim, grp, ch in picks:
            aligned_list.append({
                "challenge_group": grp,
                "challenge": ch,
                "rationale": _short_rationale(title, ch, sim),
                "alignment_strength": _strength(sim)
            })

        gaps: List[str] = []
        if not aligned_list:
            gaps.append("No suitable taxonomy match found based on lexical/semantic overlap.")
        else:
            # If best match is weak, add a gap explaining uncertainty
            if _strength(picks[0][0]) == "Weak":
                gaps.append("Top match confidence is low; taxonomy may not cover this issue precisely.")

        alignments.append({
            "issue_title": title,
            "issue_category": category,
            "aligned_challenges": aligned_list,
            "gaps": gaps
        })

    return {
        "company": company,
        "time_window": time_window,
        "alignments": alignments
    }

# ----------------------------
# In-process callable for orchestrator
# ----------------------------

def run_step(
    *,
    issues: dict,
    taxonomy: dict,
    top_k: int = 3,
    company: Optional[str] = None,
    time_window: Optional[str] = None,
) -> dict:
    """
    Thin wrapper around align_issues_to_taxonomy() so run.py can call this
    step in-process (no subprocess, no disk I/O). Preserves existing logic.
    """
    # Make a shallow copy to avoid mutating caller's object when overriding
    issues_local = dict(issues or {})
    if company:
        issues_local["company"] = company
    if time_window:
        issues_local["time_window"] = time_window
    return align_issues_to_taxonomy(issues_local, taxonomy, top_k=max(1, int(top_k or 1)))

# ----------------------------
# Markdown rendering (optional)
# ----------------------------

def render_markdown(align_json: dict) -> str:
    lines = []
    lines.append(f"# Step 4: Issue–Challenge Alignment for {align_json.get('company','Unknown')}")
    lines.append(f"_Time window: {align_json.get('time_window','Unknown')}_\n")
    aligns = align_json.get("alignments") or []
    if not aligns:
        lines.append("> No issues to align.")
        return "\n".join(lines)

    for i, row in enumerate(aligns, 1):
        lines.append(f"## {i}. {row.get('issue_title','Untitled')}  ")
        lines.append(f"**Issue Category:** {row.get('issue_category','Unknown')}\n")
        ac = row.get("aligned_challenges") or []
        if ac:
            lines.append("**Aligned Challenges:**")
            for a in ac:
                lines.append(f"- **{a.get('challenge_group','group')} → {a.get('challenge','')}**  \n"
                             f"  _Strength:_ {a.get('alignment_strength','')}  \n"
                             f"  _Rationale:_ {a.get('rationale','')}")
        else:
            lines.append("**Aligned Challenges:** _None_")
        gaps = row.get("gaps") or []
        if gaps:
            lines.append("**Gaps:**")
            for g in gaps:
                lines.append(f"- {g}")
        lines.append("")
    return "\n".join(lines)

# ----------------------------
# CLI
# ----------------------------

def _load_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)

def main():
    ap = argparse.ArgumentParser(description="Step 4: Issue–Challenge Alignment (deterministic)")
    ap.add_argument("--session-dir", required=False, help="Session directory (enables default inputs/outputs)")
    ap.add_argument("--issues", required=False, help="Path to Step-3 hypotheses JSON (problems schema)")
    ap.add_argument("--taxonomy", required=False, help="Path to campaign/customer challenges JSON")
    ap.add_argument("--company", required=False, help="Optional company override")
    ap.add_argument("--time-window", required=False, help="Optional time-window override")
    ap.add_argument("--out-json", required=False, help="Output path for Step-C alignment JSON")
    ap.add_argument("--out-md", required=False, help="Optional Markdown summary path")
    ap.add_argument("--emit-md", action="store_true", help="Emit Markdown to default session path if --out-md not provided")
    ap.add_argument("--top-k", type=int, default=3, help="Top-K aligned challenges per issue (default 3)")
    args = ap.parse_args()

    # Resolve session-aware defaults / validation
    session_dir = Path(args.session_dir).expanduser().resolve() if args.session_dir else None

    if session_dir:
        # Inputs
        issues_path = Path(args.issues).expanduser().resolve() if args.issues else (session_dir / "hypotheses.step3.json")
        taxonomy_path = Path(args.taxonomy).expanduser().resolve() if args.taxonomy else (session_dir / "campaign_challenges.json")
        # Outputs
        out_json_path = Path(args.out_json).expanduser().resolve() if args.out_json else (session_dir / "alignments.step4.json")
        out_md_path = Path(args.out_md).expanduser().resolve() if args.out_md else ((session_dir / "alignments.step4.md") if args.emit_md else None)
    else:
        # Legacy explicit mode: require explicit inputs + out-json
        if not args.issues or not args.taxonomy or not args.out_json:
            raise SystemExit("In explicit mode, --issues, --taxonomy, and --out-json are required (or pass --session-dir).")
        issues_path = Path(args.issues).expanduser().resolve()
        taxonomy_path = Path(args.taxonomy).expanduser().resolve()
        out_json_path = Path(args.out_json).expanduser().resolve()
        out_md_path = Path(args.out_md).expanduser().resolve() if args.out_md else None

    issues = _load_json(_require_file(issues_path, "Issues JSON"))
    taxonomy = _load_json(_require_file(taxonomy_path, "Taxonomy JSON"))

    # Optional overrides
    if args.company:
        issues["company"] = args.company
    if args.time_window:
        issues["time_window"] = args.time_window

    aligned = align_issues_to_taxonomy(issues, taxonomy, top_k=max(1, args.top_k))

    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json_path, "w", encoding="utf-8") as fh:
        json.dump(aligned, fh, indent=2, ensure_ascii=False)
    print(f"[Step 4] Wrote alignment JSON → {out_json_path}")
    print(f"__STEP4_JSON_PATH__:{out_json_path}")

    if out_md_path:
        md = render_markdown(aligned)
        out_md_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_md_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"[Step 4] Wrote Markdown summary → {out_md_path}")
        print(f"__STEP4_MD_PATH__:{out_md_path}")

    align_count = len(aligned.get("alignments") or [])
    print(f"__STEP4_ALIGN_COUNT__:{align_count}")

if __name__ == "__main__":
    main()