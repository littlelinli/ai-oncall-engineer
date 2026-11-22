#!/bin/bash
# Обновляет приём данных (Lambda kezekshi-ingest) до версии 2. Установщик агента обновляет terraform apply.
# Запускать в AWS CloudShell из корня репозитория:  bash scripts/deploy_agent_v2.sh
set -euo pipefail
export AWS_REGION=eu-north-1 AWS_DEFAULT_REGION=eu-north-1
FN=kezekshi-ingest
BUCKET=kezekshi-agent-788383723111
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK=$(mktemp -d)

echo "1/2 Lambda $FN"
HANDLER=$(aws lambda get-function-configuration --function-name "$FN" --query Handler --output text)
MODULE="${HANDLER%.*}"                      # например kezekshi_ingest или lambda_function
[ "${HANDLER##*.}" = "lambda_handler" ] || { echo "Неожиданный handler: $HANDLER" >&2; exit 1; }
cp "$ROOT/src/lambda/ingest/lambda_function.py" "$WORK/$MODULE.py"
(cd "$WORK" && zip -q fn.zip "$MODULE.py")
aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$WORK/fn.zip" >/dev/null
aws lambda wait function-updated --function-name "$FN"
echo "   код обновлён (handler $HANDLER)"

# Установщик агента в S3 обновляет Terraform (terraform/agent.tf): он же вписывает в него
# адрес /ingest. Копировать install.sh в бакет руками нельзя: адрес останется пустым,
# и новые агенты не будут отправлять данные.
echo "2/2 установщик агента: terraform apply (terraform/agent.tf)"
rm -rf "$WORK"
echo "agent v2 deploy done"
