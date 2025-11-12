# ---- Configurable variables ----
.DEFAULT_GOAL := help
REGION ?= us-central1
PORT ?= 8080

# Vertex AI cross-project config (calls go to portend-sam while code runs elsewhere)
VERTEX_PROJECT ?= portend-sam
VERTEX_LOCATION ?= us-central1
GOOGLE_APPLICATION_CREDENTIALS ?=

# Default Cloud Function names
GCF_NAME ?= samaia-api
GCF_NAME_SUPABASE ?= samaia-api-supabase

# CORS + Supabase (override via environment or `make VAR=value ...`)
ALLOWED_ORIGINS ?= *
SUPABASE_URL ?= https://gmgdfpdovsuzyqbyjdca.supabase.co
SUPABASE_SERVICE_KEY ?=
SUPABASE_SCHEMA ?= public

# Optional: bearer token required by the CF (used by Lovable/clients)
RUN_TOKEN ?=

# Test UUIDs for supabase target (replace when running)
CAMPAIGN_ID ?= 04d67e16-9aea-4bba-bab2-324845ae2ded
TARGET_ACCOUNT_ID ?= 0d2832d4-f4f5-42fe-8ea5-2dbd6765a46d

# Pagination size for Supabase listing (used in local tests)
PAGE_SIZE ?= 5000
# Optional: webhook callback URL (read from .env in local/dev)
CALLBACK_URL ?=

# Default inputs for full run
INPUTS ?= user_inputs.json
MAPPING_INSIGHTS ?= mapping_insights.json
SIGNAL_CATEGORIES ?= signal_categories.json
RAW_EVENTS ?= raw_events.json
CAMPAIGN_CHALLENGES ?= campaign-challenges.json

# Convenience: if .env exists, export it for local runs
# (Targets that call python or the functions framework will source this.)
ENV_FILE := .env
# Auto-generated YAML for gcloud --env-vars-file
ENV_YAML := .env.yaml

.PHONY: help deps install lint format test run run-with-inputs run-full api \
        gcf-deploy gcf-local gcf-test gcf-deploy-supabase gcf-local-supabase gcf-test-supabase \
        check-supabase-env check-tools logs clean env-print env-print-dotenv curl-local-supabase \
        run-vertex run-full-vertex env-print-vertex gcf-deploy-vertex env-to-yaml
help:
	@echo ""
	@echo "Targets:"
	@echo "  deps                 Install essential dev tools (functions-framework, flask if needed)"
	@echo "  install              Install runtime and dev requirements"
	@echo "  lint                 Run ruff + mypy"
	@echo "  format               Run black"
	@echo "  test                 Run pytest"
	@echo "  run                  Run CLI main (loads .env if present)"
	@echo "  run-with-inputs      Run CLI with --inputs $(INPUTS)"
	@echo "  run-full             Run CLI with full file arguments"
	@echo "  run-vertex           Run CLI forcing Vertex project $(VERTEX_PROJECT) / location $(VERTEX_LOCATION)"
	@echo "  run-full-vertex      Run CLI with full args, forcing Vertex project $(VERTEX_PROJECT)"
	@echo "  env-print-vertex     Show Vertex-related env (project, location, creds path)"
	@echo "  gcf-deploy-vertex    Deploy Gen2 function with Vertex env (project/location) set"
	@echo "  api                  Run uvicorn dev API server (hot reload)"
	@echo "  gcf-deploy           Deploy Gen2 HTTP function (in-repo source handler)"
	@echo "  gcf-local            Run functions-framework locally for gcf_http"
	@echo "  gcf-test             curl test against local gcf_http"
	@echo "  gcf-deploy-supabase  Deploy Gen2 HTTP function (Supabase-backed handler)"
	@echo "  gcf-local-supabase   Run functions-framework locally for gcf_http_supabase"
	@echo "  gcf-test-supabase    curl test against local Supabase handler"
	@echo "  gcf-test-supabase-all  curl test against local Supabase handler (campaign-only; processes all target accounts)"
	@echo "  curl-local-supabase-all  Curl the local Supabase handler with only CAMPAIGN_ID (fan-out over all target accounts)"
	@echo "  env-to-yaml          Convert .env (dotenv) to .env.yaml (YAML) for gcloud"
	@echo "  curl-local-supabase-webhook  Curl local Supabase handler with CAMPAIGN_ID + CALLBACK_URL from .env"
	@echo "      (uses Authorization: Bearer RUN_TOKEN automatically when RUN_TOKEN is set)"
	@echo "  logs                 Tail recent logs for deployed function(s)"
	@echo "  clean                Remove Python caches/build artifacts"
	@echo "  env-print            Show key env values (masks secrets)"
	@echo "  env-print-dotenv    Source .env, then show env values (masks secrets)"
	@echo ""

deps:
	@pip install --quiet --upgrade pip
	@pip install --quiet functions-framework flask || true

install:
	pip install -r requirements.txt
	@if [ -f requirements-dev.txt ]; then pip install -r requirements-dev.txt; fi

lint:
	ruff check src tests
	mypy src tests

format:
	black src tests

test:
	pytest -q

# ---------- Local CLI runs ----------
run:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	export PORT=$(PORT); \
	PYTHONPATH=$$PWD python -m src.app.run

run-with-inputs:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	export PORT=$(PORT); \
	PYTHONPATH=$$PWD python -m src.app.run --inputs $(INPUTS)

run-full:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	export PORT=$(PORT); \
	PYTHONPATH=$$PWD python -m src.app.run \
	  --inputs $(INPUTS) \
	  --mapping-insights $(MAPPING_INSIGHTS) \
	  --signal-categories $(SIGNAL_CATEGORIES) \
	  --raw-events $(RAW_EVENTS) \
	  --campaign-challenges $(CAMPAIGN_CHALLENGES)

run-vertex:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	export PORT=$(PORT); \
	[ -z "$(GOOGLE_APPLICATION_CREDENTIALS)" ] || export GOOGLE_APPLICATION_CREDENTIALS="$(GOOGLE_APPLICATION_CREDENTIALS)"; \
	export GOOGLE_CLOUD_PROJECT="$(VERTEX_PROJECT)"; \
	export VERTEX_LOCATION="$(VERTEX_LOCATION)"; \
	PYTHONPATH=$$PWD python -m src.app.run

run-full-vertex:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	export PORT=$(PORT); \
	[ -z "$(GOOGLE_APPLICATION_CREDENTIALS)" ] || export GOOGLE_APPLICATION_CREDENTIALS="$(GOOGLE_APPLICATION_CREDENTIALS)"; \
	export GOOGLE_CLOUD_PROJECT="$(VERTEX_PROJECT)"; \
	export VERTEX_LOCATION="$(VERTEX_LOCATION)"; \
	PYTHONPATH=$$PWD python -m src.app.run \
	  --inputs $(INPUTS) \
	  --mapping-insights $(MAPPING_INSIGHTS) \
	  --signal-categories $(SIGNAL_CATEGORIES) \
	  --raw-events $(RAW_EVENTS) \
	  --campaign-challenges $(CAMPAIGN_CHALLENGES)

api:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	uvicorn src.app.api:app --reload --port $(PORT)

# ---------- Google Cloud Function: in-repo handler ----------
gcf-deploy:
	gcloud functions deploy $(GCF_NAME) \
	  --gen2 \
	  --runtime python312 \
	  --region $(REGION) \
	  --entry-point http_handler \
	  --source . \
	  --trigger-http \
	  --allow-unauthenticated \
	  --timeout 540s \
	  --memory 2Gi \
	  --set-env-vars "CORS_ALLOW_ORIGINS=$(ALLOWED_ORIGINS),RUN_TOKEN=$(RUN_TOKEN)"

gcf-deploy-vertex:
	gcloud functions deploy $(GCF_NAME) \
	  --gen2 \
	  --runtime python312 \
	  --region $(REGION) \
	  --entry-point http_handler \
	  --source . \
	  --trigger-http \
	  --allow-unauthenticated \
	  --timeout 540s \
	  --memory 2Gi \
	  --set-env-vars "CORS_ALLOW_ORIGINS=$(ALLOWED_ORIGINS),GOOGLE_CLOUD_PROJECT=$(VERTEX_PROJECT),VERTEX_LOCATION=$(VERTEX_LOCATION)"

gcf-local:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	export PORT=$(PORT); \
	functions-framework --source gcf_http.py --target=http_handler --port=$(PORT)

gcf-test:
	@echo "POST http://localhost:$(PORT)"
	@curl -s -X POST http://localhost:$(PORT) \
	  -H "Content-Type: application/json" \
	  -d '{"inputs":{"test":"value"}}' | (jq . 2>/dev/null || cat)

# ---------- Google Cloud Function: Supabase-backed handler ----------
check-supabase-env:
	@if [ -z "$(SUPABASE_URL)" ] || [ -z "$(SUPABASE_SERVICE_KEY)" ]; then \
	  echo "Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set for Supabase targets."; \
	  echo "       e.g. make gcf-deploy-supabase SUPABASE_URL=... SUPABASE_SERVICE_KEY=..."; \
	  exit 1; \
	fi


gcf-deploy-supabase: check-supabase-env
	gcloud functions deploy $(GCF_NAME_SUPABASE) \
	  --gen2 \
	  --runtime python312 \
	  --region $(REGION) \
	  --entry-point http_handler \
	  --source . \
	  --trigger-http \
	  --allow-unauthenticated \
	  --timeout 540s \
	  --memory 2Gi \
	  --set-env-vars "CORS_ALLOW_ORIGINS=$(ALLOWED_ORIGINS),SUPABASE_URL=$(SUPABASE_URL),SUPABASE_SERVICE_KEY=$(SUPABASE_SERVICE_KEY),SUPABASE_SCHEMA=$(SUPABASE_SCHEMA),RUN_TOKEN=$(RUN_TOKEN)"

# Deploy Supabase handler with webhook (no Secret Manager)
gcf-deploy-supabase-webhook: check-project
	@if [ ! -f ".env" ]; then \
	  echo "❌ Missing .env file. Create one with WEBHOOK_SECRET, CALLBACK_URL, etc."; \
	  exit 1; \
	fi; \
	set -a; . ".env"; set +a; \
	if [ -z "$$SUPABASE_URL" ] || [ -z "$$SUPABASE_SERVICE_KEY" ]; then \
	  echo "❌ Missing SUPABASE_URL or SUPABASE_SERVICE_KEY after sourcing .env"; \
	  echo "   Fix your .env or pass them inline: SUPABASE_URL=... SUPABASE_SERVICE_KEY=... make gcf-deploy-supabase-webhook"; \
	  exit 1; \
	fi; \
	echo "🚀 Deploying $(GCF_NAME_SUPABASE) using .env (no Secret Manager, includes webhook)"; \
	gcloud functions deploy $(GCF_NAME_SUPABASE) \
	  --gen2 \
	  --runtime python312 \
	  --region $(REGION) \
	  --entry-point http_handler \
	  --source . \
	  --trigger-http \
	  --allow-unauthenticated \
	  --timeout 540s \
	  --memory 2Gi \
	  --set-env-vars "CORS_ALLOW_ORIGINS=$$CORS_ALLOW_ORIGINS,SUPABASE_URL=$$SUPABASE_URL,SUPABASE_SERVICE_KEY=$$SUPABASE_SERVICE_KEY,SUPABASE_KEY=$$SUPABASE_SERVICE_KEY,SUPABASE_SCHEMA=$$SUPABASE_SCHEMA,WEBHOOK_SECRET=$$WEBHOOK_SECRET,RUN_TOKEN=$$RUN_TOKEN,CALLBACK_URL=$$CALLBACK_URL"; \
	echo "✅ Deployed $(GCF_NAME_SUPABASE) using .env (webhook, no Secret Manager)"

gcf-local-supabase:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	export PORT=$(PORT); \
	export WEB_QUERY_PACK=configs/web_queries.generic.json; \
	export WEB_MAX_RESULTS=25; \
	export EVIDENCE_STRICTNESS=loose; \
	SUPABASE_KEY="$$SUPABASE_SERVICE_KEY" \
	functions-framework --source gcf_http_supabase.py --target=http_handler --port=$(PORT)

gcf-test-supabase:
	@echo "POST http://localhost:$(PORT) (Supabase handler)"
	@curl -s -X POST http://localhost:$(PORT) \
	  -H "Content-Type: application/json" \
	  -d '{"campaign_id":"$(CAMPAIGN_ID)","target_account_id":"$(TARGET_ACCOUNT_ID)","page_size":$(PAGE_SIZE)}' | (jq . 2>/dev/null || cat)

# Test campaign-only flow (fan-out over all target accounts)
gcf-test-supabase-all:
	@echo "POST http://localhost:$(PORT) (Supabase handler; campaign-only/all-TAs)"
	@curl -s -X POST http://localhost:$(PORT) \
	  -H "Content-Type: application/json" \
	  -d '{"campaign_id":"$(CAMPAIGN_ID)","page_size":$(PAGE_SIZE)}' | (jq . 2>/dev/null || cat)


# ---------- Ops helpers ----------
logs:
	@echo "Showing last 200 log lines for $(GCF_NAME) and $(GCF_NAME_SUPABASE) in $(REGION)"
	-@gcloud functions logs read $(GCF_NAME) --gen2 --region $(REGION) --limit=200 --format="value(textPayload)" || true
	-@gcloud functions logs read $(GCF_NAME_SUPABASE) --gen2 --region $(REGION) --limit=200 --format="value(textPayload)" || true

clean:
	@find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -name ".pytest_cache" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------- Utilities ----------
env-print:
	printf "REGION=\t\t%s\n" "$(REGION)"; \
	printf "PORT=\t\t%s\n" "$(PORT)"; \
	printf "SUPABASE_URL=\t%s\n" "$(SUPABASE_URL)"; \
	printf "SUPABASE_SERVICE_KEY=\t"; \
	if [ -n "$(SUPABASE_SERVICE_KEY)" ]; then \
	  keylen=$$(printf "%s" "$(SUPABASE_SERVICE_KEY)" | wc -c | tr -d ' '); \
	  printf "%s… (len=%s)\n" "$$(printf "%s" "$(SUPABASE_SERVICE_KEY)" | cut -c1-6)" "$$keylen"; \
	else \
	  echo "<empty>"; \
	fi; \
	printf "CAMPAIGN_ID=\t%s\n" "$(CAMPAIGN_ID)"; \
	printf "TARGET_ACCOUNT_ID=\t%s\n" "$(TARGET_ACCOUNT_ID)"; \
	printf "CALLBACK_URL=\t%s\n" "$(CALLBACK_URL)"; \
	printf "WEBHOOK_SECRET=\t"; \
	if [ -n "$(WEBHOOK_SECRET)" ]; then \
	  wlen=$$(printf "%s" "$(WEBHOOK_SECRET)" | wc -c | tr -d ' '); \
	  printf "%s… (len=%s)\n" "$$([ $${wlen} -gt 4 ] && printf "%s" "$(WEBHOOK_SECRET)" | cut -c1-4 || printf "****")" "$$wlen"; \
	else \
	  echo "<empty>"; \
	fi; \
	printf "RUN_TOKEN=\t"; \
	if [ -n "$(RUN_TOKEN)" ]; then \
	  rlen=$$(printf "%s" "$(RUN_TOKEN)" | wc -c | tr -d ' '); \
	  printf "%s… (len=%s)\n" "$$([ $${rlen} -gt 4 ] && printf "%s" "$(RUN_TOKEN)" | cut -c1-4 || printf "****")" "$$rlen"; \
	else \
	  echo "<empty>"; \
	fi

env-print-dotenv:
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	$(MAKE) --no-print-directory env-print

env-print-vertex:
	printf "VERTEX_PROJECT=\t\t%s\n" "$(VERTEX_PROJECT)"; \
	printf "VERTEX_LOCATION=\t%s\n" "$(VERTEX_LOCATION)"; \
	printf "GOOGLE_APPLICATION_CREDENTIALS=\t"; \
	if [ -n "$(GOOGLE_APPLICATION_CREDENTIALS)" ]; then \
	  base=$$(basename "$(GOOGLE_APPLICATION_CREDENTIALS)"); \
	  printf "%s (…/%s)\n" "<set>" "$$base"; \
	else \
	  echo "<empty>"; \
	fi

# Convert dotenv (.env) to YAML (.env.yaml) for gcloud --env-vars-file
# - Excludes SUPABASE_SERVICE_KEY because it is provided via Secret Manager in deploy targets

.PHONY: env-to-yaml
env-to-yaml:
	@if [ ! -f "$(ENV_FILE)" ]; then \
	  echo "❌ Missing $(ENV_FILE). Create it first."; \
	  exit 1; \
	fi; \
	echo "🔧 Generating $(ENV_YAML) from $(ENV_FILE) (excluding SUPABASE_SERVICE_KEY)"; \
	{ \
	  echo "# generated from .env"; \
	  while IFS= read -r line; do \
	    case "$$line" in \
	      ''|\#*) continue ;; \
	    esac; \
	    key="$${line%%=*}"; val="$${line#*=}"; \
	    key="$${key## }"; key="$${key%% }"; \
	    if [ "$$key" = "SUPABASE_SERVICE_KEY" ]; then continue; fi; \
	    # strip surrounding quotes if present \
	    case "$$val" in \
	      \"*\") val="$${val%\"}"; val="$${val#\"}" ;; \
	      \"\"*\"\") val="$${val%\"}"; val="$${val#\"}" ;; \
	      \"\'\"*\"\'\"\") val="$${val%\'"'"'}"; val="$${val#'"'"'"}" ;; \
	    esac; \
	    python3 -c 'import json,sys; k=sys.argv[1]; v=sys.argv[2]; print(f"{k}: {json.dumps(v)}")' "$$key" "$$val"; \
	  done < "$(ENV_FILE)"; \
	} > "$(ENV_YAML)"; \
	echo "✅ Wrote $(ENV_YAML)"

# GCP project used for secret-based deploys (set at runtime: make … PROJECT=my-project)
PROJECT ?=

check-project:
	@if [ -z "$(PROJECT)" ]; then \
	  echo "Error: Set PROJECT=<your-gcp-project> to use this target."; \
	  exit 1; \
	fi


# Convenience curl against local Supabase handler using the IDs in this Makefile
curl-local-supabase:
	@echo "POST http://localhost:$(PORT)"
	@curl -s -X POST http://localhost:$(PORT) \
	  -H "Content-Type: application/json" \
	  -d '{"campaign_id":"$(CAMPAIGN_ID)","target_account_id":"$(TARGET_ACCOUNT_ID)","page_size":$(PAGE_SIZE)}' | (jq . 2>/dev/null || cat)

# Convenience curl: campaign-only fan-out over all target accounts
curl-local-supabase-all:
	@echo "POST http://localhost:$(PORT) (campaign-only/all-TAs)"
	@curl -s -X POST http://localhost:$(PORT) \
	  -H "Content-Type: application/json" \
	  -d '{"campaign_id":"$(CAMPAIGN_ID)","page_size":$(PAGE_SIZE)}' | (jq . 2>/dev/null || cat)

# Convenience curl: campaign-only fan-out with webhook callback URL from .env
curl-local-supabase-webhook:
	@echo "POST http://localhost:$(PORT) (campaign-only/all-TAs with webhook)"
	@if [ -f "$(ENV_FILE)" ]; then set -a; . "$(ENV_FILE)"; set +a; fi; \
	AUTH_HDR=""; \
	if [ -n "$$RUN_TOKEN" ]; then AUTH_HDR="Authorization: Bearer $$RUN_TOKEN"; fi; \
	curl -s -X POST http://localhost:$(PORT) \
	  -H "Content-Type: application/json" \
	  $${AUTH_HDR:+-H "$$AUTH_HDR"} \
	  -d '{"campaign_id":"$(CAMPAIGN_ID)","page_size":$(PAGE_SIZE),"callback_url":"$(CALLBACK_URL)"}' | (jq . 2>/dev/null || cat)