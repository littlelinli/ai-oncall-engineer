#!/usr/bin/env bash
# Starts the demo server. The IP does not change (Elastic IP); agent and logs are kept.
# Usage: ./scripts/demo-up.sh  (extra arguments go to terraform, e.g. -auto-approve)
set -euo pipefail
cd "$(dirname "$0")/../terraform"
[ -d .terraform ] || terraform init

terraform apply -var-file=envs/dev.tfvars -var=app_running=true "$@"

url="$(terraform output -raw app_url)"
echo "Waiting for the app to answer on $url/health ..."
for _ in $(seq 30); do
  if curl -fsS -m 5 -o /dev/null "$url/health"; then
    echo "Ready: $url"
    exit 0
  fi
  sleep 10
done
echo "The server is running but the app has not answered for 5 minutes. Check: terraform output instance_id, then aws ssm start-session --target <id>" >&2
exit 1
