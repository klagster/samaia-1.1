
# SAMaiA 5‑Step ADK Pipeline  
_End‑to‑end campaign intelligence using Google ADK, Vertex AI, and Supabase_

This project implements a production‑grade, session‑aware 5‑step GTM intelligence pipeline:

1. **Step 1 — Web Evidence Grabber**  
   Grounded web search using Vertex Search (or NO‑OP fallback).  
   Code: `src/app/step1_evidence_grabber.py`  
   Docs: ADK Tools → Web search patterns

2. **Step 2 — Evidence Harvester**  
   Normalizes raw events, deduplicates, categorizes, computes confidence.  
   Code: `src/app/step2_evidence_harvester.py`  
   Docs: Deterministic prep transforms

3. **Step 3 — Hypotheses Generator (LLM)**  
   Generates evidenced business problems with retry logic and JSON extraction.  
   Code: `src/app/step3_hypotheses_generator.py`  
   Env tuning: `TA_STEP3_TEMPERATURE`, `TA_STEP3_MAX_OUTPUT_TOKENS`  
   Docs: ADK LLM Gen, Retry patterns

4. **Step 4 — Alignment (Deterministic)**  
   Maps problems → campaign taxonomy using hybrid similarity + rationale.  
   Code: `src/app/step4_alignment.py`

5. **Step 5 — Compelling Events (LLM)**  
   Generates final CE messages with evidence scoring + retry logic.  
   Code: `src/app/step5_compelling_events.py`  
   Env tuning: `TA_STEP5_TEMPERATURE`, `TA_STEP5_MAX_OUTPUT_TOKENS`

---

## Architecture  
See `docs/architecture.md` for the full system diagram.

Key components:
- **run.py Orchestrator**: ADK‑session aware, async‑safe, posts webhooks, writes to Supabase.  
- **docsmap.yaml**: Contract between this repo and ADK official docs (required to keep guidance aligned).  
- **Supabase Integration**: Each step writes results into `target_accounts` using PostgREST PATCH.  
- **Cloud Run / Cloud Functions**: Execution environment, concurrency‑safe, RUN_ID propagated through pipeline.

---

## ADK Documentation Contract  
This project uses a docs contract (`docsmap.yaml`) that maps:
- Each step → required ADK features  
- Trusted sources → `/docs`, `/examples`, `/reference`  
- Live API references → https://google.github.io/adk-docs/api-reference/  
- Quickstart → https://google.github.io/adk-docs/get-started/quickstart/

The orchestrator uses ADK session primitives:
- `InMemorySessionService`
- `Session`
- `Event`, `EventActions`
- `Content`, `Part`

---

## Quick Start  
```bash
python -m venv .venv
source .venv/bin/activate

make install
make run
```

Inputs: place JSON in `.inputs/` or POST via API.

Outputs: written to `.outputs/` and Supabase (`analysis_updated_at` automatically updated).

---

## Environment Variables  
Minimal:
```
SUPABASE_URL
SUPABASE_SERVICE_KEY
GOOGLE_CLOUD_PROJECT
GOOGLE_CLOUD_LOCATION
```

Per‑step LLM controls:
```
TA_STEP3_TEMPERATURE
TA_STEP3_MAX_OUTPUT_TOKENS
TA_STEP5_TEMPERATURE
TA_STEP5_MAX_OUTPUT_TOKENS
```

---

## Repository Layout  
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
docsmap.yaml
```

---

## Notes  
- All LLM steps include exponential backoff retry protection.  
- Steps 2 and 4 are deterministic and do not use LLMs.  
- JSON salvage is implemented for Steps 3 and 5 to tolerate malformed model output.  
