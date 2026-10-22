#!/usr/bin/env bash
# Deploy do adapter no Google Cloud Run com um comando: ./deploy-gcp.sh
# Pré-requisitos: gcloud instalado, `gcloud auth login` feito e um projeto com billing ativo
# (GCP_PROJECT ou `gcloud config set project <id>`). Lê TMS_HOST, TMS_PORT e TMS_TOKEN do .env
# (ou do ambiente). O token vira secret no Secret Manager e nunca entra na imagem.
# Variáveis opcionais: APP_NAME, GCP_PROJECT, REGION.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] && { set -a; . ./.env; set +a; }
: "${TMS_HOST:?defina TMS_HOST}" "${TMS_PORT:?defina TMS_PORT}" "${TMS_TOKEN:?defina TMS_TOKEN}"
APP_NAME="${APP_NAME:-tms-adapter}"
REGION="${REGION:-us-central1}"   # próximo de Chicago
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
: "${PROJECT:?defina GCP_PROJECT ou rode: gcloud config set project <id>}"
SECRET="${APP_NAME}-tms-token"

# 1) APIs necessárias.
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com --project "$PROJECT" >/dev/null

# 2) Token como secret (cria ou adiciona nova versão) e acesso para a conta do Cloud Run.
if gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  printf '%s' "$TMS_TOKEN" | gcloud secrets versions add "$SECRET" --project "$PROJECT" --data-file=- >/dev/null
else
  printf '%s' "$TMS_TOKEN" | gcloud secrets create "$SECRET" --project "$PROJECT" --data-file=- >/dev/null
fi
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT" \
  --member "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor >/dev/null

# 3) Build da imagem (Dockerfile) e deploy com URL pública HTTPS.
#    Instância única: o lock/estado das reservas é em memória. CPU sempre alocada para manter o servidor responsivo.
gcloud run deploy "$APP_NAME" --project "$PROJECT" --region "$REGION" --source . \
  --allow-unauthenticated --port 8080 --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --set-env-vars "HOST=0.0.0.0,TMS_HOST=$TMS_HOST,TMS_PORT=$TMS_PORT" \
  --set-secrets "TMS_TOKEN=${SECRET}:latest"

# 4) Smoke test.
URL=$(gcloud run services describe "$APP_NAME" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
echo "URL: $URL"
curl -fsS "$URL/healthz" && echo && curl -fsS -H "Authorization: Bearer $TMS_TOKEN" "$URL/health"
