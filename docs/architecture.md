

# SAMaiA Architecture Overview

This document provides a high‑level overview of the SAMaiA 5‑step pipeline and the orchestration layer.

---

## 🧠 Pipeline Overview (5 Steps)

### **Step 1 — Evidence Grabber**
- Async web evidence collector (Vertex Search or fallback no‑op).
- Produces raw web events: title, url, snippet, date, publisher, query.

### **Step 2 — Evidence Harvester**
- Normalizes Step 1 output.
- Deduplicates, assigns categories, computes confidence.
- Outputs: `{ company, generated_at, evidence[] }`.

### **Step 3 — Hypotheses Generator**
- Uses Gemini (google‑genai) with retry/backoff.
- Synthesizes evidenced, executive‑ready business problems.
- Normalizes evidence entries and categories.
- Output schema: `problems[]` with evidence and metadata.

### **Step 4 — Issue→Challenge Alignment**
- Deterministic, no‑LLM.
- Builds NLP-weighted features for each problem and taxonomy challenge.
- Produces aligned challenges and gap analysis.

### **Step 5 — Compelling Events Generator**
- Uses Gemini with retry/backoff.
- Generates compelling event narratives grounded in evidence + alignments.
- Produces stakeholder‑specific triggers, risks, opportunities.

---

## ⚙️ Orchestrator (`run.py`)
- Session‑aware: uses ADK session state.
- Executes each step in-process if available; otherwise subprocess fallback.
- Persists each step output to Supabase (`target_accounts` columns).
- Emits webhook updates signed with HMAC SHA-256.
- Manages `RUN_ID`, environment config, output directory management.

---

## 🗂 Data Flow Summary

```
inputs.json
   ↓
Step 1 → raw_events.json
   ↓
Step 2 → evidence_index.json
   ↓
Step 3 → hypotheses.step3.json
   ↓
Step 4 → alignments.step4.json
   ↓
Step 5 → compelling_events.step5.json
```

---

## 🧩 Key Design Notes

- **Async-first**: Step 1 uses async collectors; orchestrator uses asyncio for non-blocking subprocess handling.
- **LLM hardened**: Steps 3 & 5 use tenacity retry logic for 429 / RESOURCE_EXHAUSTED.
- **Normalization backbone**: Many helpers normalize URLs, dates, evidence objects.
- **Supabase persistence**: Each step saves into its own JSONB column.
- **Cloud-friendly**: Works in Cloud Functions, Cloud Run, or purely local.

---

## 📦 Folder Structure

```
src/app/
  run.py
  step1_evidence_grabber.py
  step2_evidence_harvester.py
  step3_hypotheses_generator.py
  step4_alignment.py
  step5_compelling_events.py
docs/
  architecture.md
configs/
  web_queries.combined.json
```

---

## 🚀 Purpose

SAMaiA turns grounded signals → structured GTM intelligence with fully automated orchestration, evidence traceability, and stable downstream outputs.