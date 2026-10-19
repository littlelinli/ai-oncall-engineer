#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/../terraform"
terraform destroy -var-file=envs/dev.tfvars
