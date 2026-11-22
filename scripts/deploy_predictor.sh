#!/bin/bash
# Разворачивает ML-агента Kezekshi (Lambda kezekshi-predictor) в AWS.
# Запускать в AWS CloudShell из корня репозитория:  bash scripts/deploy_predictor.sh
# Повторный запуск безопасен: существующее не пересоздаётся, код Lambda обновляется.
set -euo pipefail

export AWS_REGION=eu-north-1 AWS_DEFAULT_REGION=eu-north-1
FN=kezekshi-predictor
ROLE=kezekshi-predictor-role
TABLE=kezekshi-predictions
SERVERS_TABLE=incident-demo-servers
ANALYZER=incident-analyzer
RULE=kezekshi-predictor-every-5-min
SRC="$(cd "$(dirname "$0")/../src/lambda/predictor" && pwd)"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
WORK=$(mktemp -d)

echo "1/6 таблица прогнозов $TABLE"
if ! aws dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "$TABLE" \
    --attribute-definitions AttributeName=server_id,AttributeType=S AttributeName=ts,AttributeType=S \
    --key-schema AttributeName=server_id,KeyType=HASH AttributeName=ts,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE"
  aws dynamodb update-time-to-live --table-name "$TABLE" \
    --time-to-live-specification Enabled=true,AttributeName=expires_at >/dev/null
fi

echo "2/6 роль $ROLE"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  NEW_ROLE=1
fi
cat > "$WORK/policy.json" <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["cloudwatch:GetMetricData","cloudwatch:ListMetrics","cloudwatch:PutMetricData"],"Resource":"*"},
 {"Effect":"Allow","Action":["dynamodb:GetItem","dynamodb:PutItem"],"Resource":"arn:aws:dynamodb:$AWS_REGION:$ACCOUNT:table/$TABLE"},
 {"Effect":"Allow","Action":["dynamodb:Scan"],"Resource":"arn:aws:dynamodb:$AWS_REGION:$ACCOUNT:table/$SERVERS_TABLE"}
]}
EOF
aws iam put-role-policy --role-name "$ROLE" --policy-name predictor-access --policy-document "file://$WORK/policy.json"
[ "${NEW_ROLE:-0}" = 1 ] && { echo "   жду 15 сек, пока роль применится"; sleep 15; }

echo "3/6 настройки Telegram берём из $ANALYZER"
aws lambda get-function-configuration --function-name "$ANALYZER" --query Environment.Variables --output json > "$WORK/analyzer_env.json"
python3 - "$WORK/analyzer_env.json" "$WORK/env.json" "$TABLE" "$SERVERS_TABLE" <<'EOF'
import json, sys
src = json.load(open(sys.argv[1])) or {}
env = {k: src[k] for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if src.get(k)}
env.update(PREDICTIONS_TABLE=sys.argv[3], SERVERS_TABLE=sys.argv[4], METRICS_NAMESPACE="Kezekshi",
           FORECAST_NAMESPACE="Kezekshi/Forecast")
print("   Telegram:", "найден" if "TELEGRAM_BOT_TOKEN" in env else "НЕ найден, предупреждения не будут уходить")
json.dump({"Variables": env}, open(sys.argv[2], "w"))
EOF

echo "4/6 функция $FN"
(cd "$SRC" && zip -q -r "$WORK/fn.zip" lambda_function.py model.py)
if aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$WORK/fn.zip" >/dev/null
  aws lambda wait function-updated --function-name "$FN"
  aws lambda update-function-configuration --function-name "$FN" --environment "file://$WORK/env.json" >/dev/null
  aws lambda wait function-updated --function-name "$FN"
else
  for i in 1 2 3 4 5; do
    aws lambda create-function --function-name "$FN" --runtime python3.12 \
      --role "arn:aws:iam::$ACCOUNT:role/$ROLE" --handler lambda_function.handler \
      --zip-file "fileb://$WORK/fn.zip" --timeout 120 --memory-size 256 \
      --environment "file://$WORK/env.json" >/dev/null && break
    echo "   роль ещё не готова, повтор через 10 сек"; sleep 10
  done
  aws lambda wait function-active-v2 --function-name "$FN"
fi

echo "5/6 расписание: раз в 5 минут"
RULE_ARN=$(aws events put-rule --name "$RULE" --schedule-expression "rate(5 minutes)" --query RuleArn --output text)
aws lambda add-permission --function-name "$FN" --statement-id events-invoke \
  --action lambda:InvokeFunction --principal events.amazonaws.com --source-arn "$RULE_ARN" >/dev/null 2>&1 || true
aws events put-targets --rule "$RULE" \
  --targets "Id=predictor,Arn=arn:aws:lambda:$AWS_REGION:$ACCOUNT:function:$FN" >/dev/null

echo "6/6 пробный запуск (ничего не отправляет)"
aws lambda invoke --function-name "$FN" --cli-binary-format raw-in-base64-out \
  --payload '{"dry_run": true}' "$WORK/out.json" >/dev/null
python3 - "$WORK/out.json" <<'EOF'
import json, sys
res = json.load(open(sys.argv[1]))
if "errorMessage" in res:
    print("ОШИБКА:", res["errorMessage"]); sys.exit(1)
print(f"серверов с данными: {len(res)}")
for sid, r in res.items():
    print(f"\n{sid}  риски: {', '.join(r['risks']) or 'нет'}")
    for k, v in r["forecast"].items():
        print(f"  {k:6s} сейчас {v['now']:7.2f}   через 15м {v['f15']:7.2f}   30м {v['f30']:7.2f}   60м {v['f60']:7.2f}   ({v['method']})")
EOF
rm -rf "$WORK"
echo
echo "predictor deploy done"
