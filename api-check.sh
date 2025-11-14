#!/usr/bin/env bash

set -e

# ----------------------------
# Required APIs for your pipeline
# ----------------------------
REQUIRED_APIS=(
  run.googleapis.com
  cloudfunctions.googleapis.com
  cloudtasks.googleapis.com
  cloudbuild.googleapis.com
  artifactregistry.googleapis.com
  iamcredentials.googleapis.com
  eventarc.googleapis.com
  logging.googleapis.com
)

# Optional but recommended
OPTIONAL_APIS=(
  cloudscheduler.googleapis.com
  secretmanager.googleapis.com
  monitoring.googleapis.com
  pubsub.googleapis.com
  cloudtrace.googleapis.com
  bigquery.googleapis.com
  billingbudgets.googleapis.com
  recommender.googleapis.com
  cloudasset.googleapis.com
)

PROJECT=""
if [ "$1" == "--project" ] && [ -n "$2" ]; then
  PROJECT="$2"
else
  echo "❌ Usage: bash check_gcp_apis.sh --project PROJECT_ID"
  exit 1
fi

echo ""
echo "🔍 Checking GCP APIs for project: $PROJECT"
echo "--------------------------------------------"

check_apis() {
  local TYPE="$1"
  shift
  local APIS=("$@")

  echo ""
  echo "=== $TYPE APIs ==="
  for API in "${APIS[@]}"; do
    if gcloud services list --enabled --project "$PROJECT" | grep -q "$API"; then
      echo "✔️  ENABLED:   $API"
    else
      echo "❌ MISSING:   $API"
      MISSING_APIS+=("$API")
    fi
  done
}

MISSING_APIS=()

# Check required + optional
check_apis "Required" "${REQUIRED_APIS[@]}"
check_apis "Optional/Recommended" "${OPTIONAL_APIS[@]}"

echo ""
echo "--------------------------------------------"
if [ ${#MISSING_APIS[@]} -eq 0 ]; then
  echo "🎉 All required APIs are enabled!"
  exit 0
else
  echo "⚠️  Missing APIs (${#MISSING_APIS[@]}):"
  for API in "${MISSING_APIS[@]}"; do
    echo "   - $API"
  done

  echo ""
  echo "➡️ To enable ALL missing APIs, run:"
  echo ""
  echo "gcloud services enable ${MISSING_APIS[*]} --project $PROJECT"
  echo ""
fi