#!/usr/bin/env bash
# Deploy do adapter no AWS App Runner com um comando: ./deploy-aws.sh
# Pré-requisitos: AWS CLI v2 e Docker instalados, credenciais configuradas (`aws configure` ou `aws sso login`).
# Lê TMS_HOST, TMS_PORT e TMS_TOKEN do .env (ou do ambiente). O token vira secret no Secrets Manager
# e nunca entra na imagem. Variáveis opcionais: APP_NAME, AWS_REGION.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] && { set -a; . ./.env; set +a; }
: "${TMS_HOST:?defina TMS_HOST}" "${TMS_PORT:?defina TMS_PORT}" "${TMS_TOKEN:?defina TMS_TOKEN}"
APP_NAME="${APP_NAME:-tms-adapter}"
export AWS_REGION="${AWS_REGION:-us-east-2}"   # Ohio, próximo de Chicago
export AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER=""
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
IMAGE="$REGISTRY/$APP_NAME:latest"
SECRET="$APP_NAME/tms-token"
ACCESS_ROLE="$APP_NAME-apprunner-ecr"
INSTANCE_ROLE="$APP_NAME-apprunner-instance"

# 1) Build da imagem (Dockerfile) e push para o ECR. App Runner roda linux/amd64.
aws ecr describe-repositories --repository-names "$APP_NAME" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$APP_NAME" >/dev/null
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

# 2) Token como secret (cria ou atualiza).
if aws secretsmanager describe-secret --secret-id "$SECRET" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "$TMS_TOKEN" >/dev/null
else
  aws secretsmanager create-secret --name "$SECRET" --secret-string "$TMS_TOKEN" >/dev/null
fi
SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRET" --query ARN --output text)

# 3) Roles: uma para o App Runner puxar a imagem do ECR, outra para a instância ler o secret.
trust() { printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"%s"},"Action":"sts:AssumeRole"}]}' "$1"; }
aws iam get-role --role-name "$ACCESS_ROLE" >/dev/null 2>&1 || {
  aws iam create-role --role-name "$ACCESS_ROLE" --assume-role-policy-document "$(trust build.apprunner.amazonaws.com)" >/dev/null
  aws iam attach-role-policy --role-name "$ACCESS_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
}
aws iam get-role --role-name "$INSTANCE_ROLE" >/dev/null 2>&1 \
  || aws iam create-role --role-name "$INSTANCE_ROLE" --assume-role-policy-document "$(trust tasks.apprunner.amazonaws.com)" >/dev/null
aws iam put-role-policy --role-name "$INSTANCE_ROLE" --policy-name read-tms-token --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"$SECRET_ARN\"}]}"
ACCESS_ARN=$(aws iam get-role --role-name "$ACCESS_ROLE" --query Role.Arn --output text)
INSTANCE_ARN=$(aws iam get-role --role-name "$INSTANCE_ROLE" --query Role.Arn --output text)

# 4) Instância única: o lock/estado das reservas é em memória.
SCALING_ARN=$(aws apprunner create-auto-scaling-configuration --auto-scaling-configuration-name "$APP_NAME-single" \
  --min-size 1 --max-size 1 --max-concurrency 100 --query AutoScalingConfiguration.AutoScalingConfigurationArn --output text)

# 5) Cria o serviço (ou só reimplanta a imagem se ele já existir).
SERVICE_ARN=$(aws apprunner list-services --query "ServiceSummaryList[?ServiceName=='$APP_NAME'].ServiceArn | [0]" --output text)
if [ "$SERVICE_ARN" = "None" ]; then
  sleep 10   # propagação do IAM
  SERVICE_ARN=$(aws apprunner create-service --service-name "$APP_NAME" \
    --auto-scaling-configuration-arn "$SCALING_ARN" \
    --instance-configuration "InstanceRoleArn=$INSTANCE_ARN" \
    --health-check-configuration "Protocol=HTTP,Path=/healthz" \
    --source-configuration "{
      \"AuthenticationConfiguration\": {\"AccessRoleArn\": \"$ACCESS_ARN\"},
      \"AutoDeploymentsEnabled\": false,
      \"ImageRepository\": {
        \"ImageIdentifier\": \"$IMAGE\",
        \"ImageRepositoryType\": \"ECR\",
        \"ImageConfiguration\": {
          \"Port\": \"8080\",
          \"RuntimeEnvironmentVariables\": {\"HOST\": \"0.0.0.0\", \"TMS_HOST\": \"$TMS_HOST\", \"TMS_PORT\": \"$TMS_PORT\"},
          \"RuntimeEnvironmentSecrets\": {\"TMS_TOKEN\": \"$SECRET_ARN\"}
        }
      }}" --query Service.ServiceArn --output text)
else
  aws apprunner start-deployment --service-arn "$SERVICE_ARN" >/dev/null
fi

# 6) Espera ficar RUNNING e faz smoke test.
echo "Aguardando o serviço ficar RUNNING..."
for _ in $(seq 1 60); do
  STATUS=$(aws apprunner describe-service --service-arn "$SERVICE_ARN" --query Service.Status --output text)
  [ "$STATUS" = "RUNNING" ] && break
  case "$STATUS" in *FAILED*) echo "Deploy falhou: $STATUS" >&2; exit 1;; esac
  sleep 10
done
[ "$STATUS" = "RUNNING" ] || { echo "Timeout aguardando o serviço (status: $STATUS)" >&2; exit 1; }
URL="https://$(aws apprunner describe-service --service-arn "$SERVICE_ARN" --query Service.ServiceUrl --output text)"
echo "URL: $URL"
curl -fsS "$URL/healthz" && echo && curl -fsS -H "Authorization: Bearer $TMS_TOKEN" "$URL/health"
