#!/bin/bash
# Обновляет incident-analyzer (правила вместо Gemini, объединение повторов) и kezekshi-predictor.
# Запускать в AWS CloudShell из корня репозитория:  bash scripts/deploy_analyzer.sh
# Повторный запуск безопасен. То же самое потом делает terraform apply (terraform/lambda.tf).
set -euo pipefail
export AWS_REGION=eu-north-1 AWS_DEFAULT_REGION=eu-north-1
FN=incident-analyzer
ROLE=incident-demo-incident-analyzer
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
WORK=$(mktemp -d)

echo "1/4 права анализатора: история инцидентов и прогнозы"
cat > "$WORK/policy.json" <<POL
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:GetItem","dynamodb:Scan"],
  "Resource":"arn:aws:dynamodb:$AWS_REGION:$ACCOUNT:table/incident-demo-incidents"},
 {"Effect":"Allow","Action":["dynamodb:GetItem"],
  "Resource":"arn:aws:dynamodb:$AWS_REGION:$ACCOUNT:table/kezekshi-predictions"}
]}
POL
aws iam put-role-policy --role-name "$ROLE" --policy-name analyzer-extra --policy-document "file://$WORK/policy.json"

echo "2/4 код $FN"
(cd "$ROOT/src/lambda/incident_processor" && zip -q "$WORK/an.zip" lambda_function.py explain.py)
aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$WORK/an.zip" >/dev/null
aws lambda wait function-updated --function-name "$FN"

echo "3/4 настройки $FN"
# демо-сервер = сервер агента, который работает на том же EC2 (ищем по имени в таблице серверов)
DEMO_ID="${1:-}"
if [ -z "$DEMO_ID" ]; then
  DEMO_ID=$(aws cloudwatch list-metrics --namespace Kezekshi --metric-name Heartbeat --recently-active PT3H \
    --query 'Metrics[].Dimensions[?Name==`ServerId`].Value[]' --output text | tr '\t' '\n' | sort -u | head -1)
fi
aws lambda get-function-configuration --function-name "$FN" --query Environment > "$WORK/env.json"
python3 - "$WORK/env.json" "$DEMO_ID" <<'PY'
import json, sys
p, demo = sys.argv[1], sys.argv[2]
e = json.load(open(p)) or {"Variables": {}}
v = e.setdefault("Variables", {})
v.pop("GEMINI_API_KEY", None)          # внешний ИИ больше не нужен, ключ не храним
v["PREDICTIONS_TABLE"] = "kezekshi-predictions"
v["DEMO_SERVER_ID"] = demo
v.setdefault("AUTO_FIX", "false")
json.dump(e, open(p, "w"))
print(f"   DEMO_SERVER_ID={demo}  AUTO_FIX={v['AUTO_FIX']}  Gemini: удалён")
PY
aws lambda update-function-configuration --function-name "$FN" --environment "file://$WORK/env.json" >/dev/null
aws lambda wait function-updated --function-name "$FN"

echo "4/4 код kezekshi-predictor (пишет текст предупреждения для анализатора)"
(cd "$ROOT/src/lambda/predictor" && zip -q "$WORK/pr.zip" lambda_function.py model.py)
aws lambda update-function-code --function-name kezekshi-predictor --zip-file "fileb://$WORK/pr.zip" >/dev/null
aws lambda wait function-updated --function-name kezekshi-predictor

rm -rf "$WORK"
echo "analyzer deploy done"
