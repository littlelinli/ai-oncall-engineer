#!/usr/bin/env bash
# Останавливает демо-сервер после показа: EC2 больше не тарифицируется.
# Остаются диск, агент, IP, API, таблицы и ссылка на install.sh. Включить обратно: ./scripts/demo-up.sh
# Запуск: ./scripts/demo-down.sh  (лишние параметры уходят в terraform, например -auto-approve)
set -euo pipefail
cd "$(dirname "$0")/../terraform"
[ -d .terraform ] || terraform init

terraform apply -var-file=envs/dev.tfvars -var=app_running=false "$@"

echo "Сервер остановлен. Важно: обычный terraform apply без -var=app_running=false снова его включит."
