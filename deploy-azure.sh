#!/usr/bin/env bash
# Deploy do adapter no Azure Container Apps com um comando: ./deploy-azure.sh
# Pré-requisitos: Azure CLI instalado e `az login` feito. Lê TMS_HOST, TMS_PORT e TMS_TOKEN do .env
# (ou do ambiente). O token vira secret do Container App e nunca entra na imagem.
# Variáveis opcionais: APP_NAME, RESOURCE_GROUP, LOCATION.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] && { set -a; . ./.env; set +a; }
: "${TMS_HOST:?defina TMS_HOST}" "${TMS_PORT:?defina TMS_PORT}" "${TMS_TOKEN:?defina TMS_TOKEN}"
APP_NAME="${APP_NAME:-tms-adapter}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-tms-adapter}"
LOCATION="${LOCATION:-centralus}"   # próximo de Chicago

# 1) Build da imagem (Dockerfile) e criação do app com ingress público HTTPS.
#    TMS_TOKEN entra como placeholder para não aparecer como valor em texto puro.
az containerapp up --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" \
  --source . --ingress external --target-port 8080 \
  --env-vars HOST=0.0.0.0 PORT=8080 "TMS_HOST=$TMS_HOST" "TMS_PORT=$TMS_PORT" TMS_TOKEN=pending

# 2) Token como secret + instância única (o lock/estado das reservas é em memória).
az containerapp secret set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --secrets "tms-token=$TMS_TOKEN" >/dev/null
az containerapp update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --set-env-vars TMS_TOKEN=secretref:tms-token --min-replicas 1 --max-replicas 1 >/dev/null

# 3) Smoke test.
FQDN=$(az containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn -o tsv)
echo "URL: https://$FQDN"
curl -fsS "https://$FQDN/healthz" && echo && curl -fsS -H "Authorization: Bearer $TMS_TOKEN" "https://$FQDN/health"
