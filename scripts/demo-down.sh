#!/usr/bin/env bash
# Stops the demo server: EC2 is no longer billed.
# Disk, agent, IP, API, tables and the install.sh link stay. Start it again: ./scripts/demo-up.sh
# Usage: ./scripts/demo-down.sh  (extra arguments go to terraform, e.g. -auto-approve)
set -euo pipefail
cd "$(dirname "$0")/../terraform"
[ -d .terraform ] || terraform init

terraform apply -var-file=envs/dev.tfvars -var=app_running=false "$@"

echo "Server stopped. Note: a plain terraform apply without -var=app_running=false starts it again."
