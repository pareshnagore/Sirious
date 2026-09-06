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
  --set-env-vars "GEMINI_API_KEY=$GEMINI_API_KEY,SIRIOUS_MODEL=$SIRIOUS_MODEL,SIRIOUS_AUTH_TOKEN=$SIRIOUS_AUTH_TOKEN,SIRIOUS_PERSIST=1,SIRIOUS_MEMORY=1,SIRIOUS_REMINDERS=1,SIRIOUS_TASKS_QUEUE=projects/sirious-2026/locations/asia-south1/queues/sirious-reminders,SIRIOUS_FIRE_URL=https://sirious-api-635321277027.asia-south1.run.app/internal/fire-reminder,SIRIOUS_FIRE_OIDC_SA=sirious-reminders-signer@sirious-2026.iam.gserviceaccount.com"
