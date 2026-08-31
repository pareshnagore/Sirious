#!/bin/bash
# Deploy sirious-api with the full env-var set (SIRIOUS_MODEL must be set
# explicitly — it lives only on Cloud Run; deploying with it unset blanks
# the pin and breaks prod).
set -e
cd /d/Hermes/Sirious/backend
set -a
source ./.env
set +a
gcloud run deploy sirious-api \
  --source . \
  --region asia-south1 \
  --no-cpu-throttling \
  --set-env-vars "GEMINI_API_KEY=$GEMINI_API_KEY,SIRIOUS_MODEL=$SIRIOUS_MODEL,SIRIOUS_AUTH_TOKEN=$SIRIOUS_AUTH_TOKEN,SIRIOUS_PERSIST=1,SIRIOUS_MEMORY=1"
