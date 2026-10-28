#!/usr/bin/env bash
# Включает демо-сервер перед показом. IP не меняется (Elastic IP), агент и логи на месте.
# Запуск: ./scripts/demo-up.sh  (лишние параметры уходят в terraform, например -auto-approve)
set -euo pipefail
cd "$(dirname "$0")/../terraform"
[ -d .terraform ] || terraform init

terraform apply -var-file=envs/dev.tfvars -var=app_running=true "$@"

url="$(terraform output -raw app_url)"
echo "Жду, пока приложение ответит на $url/health ..."
for _ in $(seq 30); do
  if curl -fsS -m 5 -o /dev/null "$url/health"; then
    echo "Готово: $url"
    exit 0
  fi
  sleep 10
done
echo "Сервер запущен, но приложение не отвечает 5 минут. Проверьте: terraform output instance_id, затем aws ssm start-session --target <id>" >&2
exit 1
