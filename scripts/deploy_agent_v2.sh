#!/bin/bash
# Обновляет приём данных (Lambda kezekshi-ingest) и установщик агента в S3 до версии 2.
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

echo "2/2 установщик агента в s3://$BUCKET"
KEYS=$(aws s3 ls "s3://$BUCKET/" --recursive | awk '{print $4}' | grep 'install\.sh$' || true)
[ -n "$KEYS" ] || KEYS=install.sh
for k in $KEYS; do
  aws s3 cp "$ROOT/src/agent/install.sh" "s3://$BUCKET/$k" --content-type text/x-sh --only-show-errors
  echo "   обновлён $k"
done
for k in $(aws s3 ls "s3://$BUCKET/" --recursive | awk '{print $4}' | grep 'kezekshi_agent\.py$' || true); do
  aws s3 cp "$ROOT/src/agent/kezekshi_agent.py" "s3://$BUCKET/$k" --only-show-errors
  echo "   обновлён $k"
done
rm -rf "$WORK"
echo "agent v2 deploy done"
