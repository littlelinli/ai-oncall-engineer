# Resources Outside Terraform

AWS account state checked on 2026-11-22, region eu-north-1. Everything not listed here is created
by `terraform/` (see the `*.tf` files). Use this list when moving to a new AWS account.

## Created manually or by scripts (must be recreated on a new account)

| Resource | Name | Created by | Notes |
|---|---|---|---|
| Lambda + Function URL | `kezekshi-api` | #4, console | Reads incidents for the dashboard. Role `kezekshi-api-role-xgmfjkd5` (console-generated). |
| S3 bucket (static website) | `kezekshi-dashboard-alikhan` | #4, console | Dashboard site. Source goes to `site/`. |
| Lambda | `kezekshi-predictor` | `scripts/deploy_predictor.sh` | Code in `src/lambda/predictor/`. |
| IAM role | `kezekshi-predictor-role` | `scripts/deploy_predictor.sh` | Inline policy `predictor-access`. |
| DynamoDB table | `kezekshi-predictions` | `scripts/deploy_predictor.sh` | |
| EventBridge rule | `kezekshi-predictor-every-5-min` | `scripts/deploy_predictor.sh` | Triggers the predictor. |
| S3 bucket | `kezekshi-dataset-<account_id>` | unknown, 2026-11-22 | Contains `shopsim/`. Owner: please add details. |
| S3 bucket (Terraform state) | `incident-demo-tfstate-<account_id>` | `terraform/bootstrap` | Bootstrap state is local only (gitignored). |

## Created at runtime (nothing to migrate)

- CloudWatch alarms `ai-oncall-engineer-srv-<server_id>-{cpu-high,disk-high,app-errors,server-down}`:
  created by the `/ingest` Lambda when a server reports for the first time.

## Outside AWS

- Gemini API key, Telegram bot token and chat id: only in local `terraform/envs/dev.tfvars`
  (gitignored). Template: `terraform/envs/dev.tfvars.example`.

## Leftovers (do not migrate, delete later)

- IAM role `incident-analyzer-role-6hr2du5k` (old console-created Lambda role).
- EC2 `i-0e2df0d664ca52f87` (`ai-oncall-engineer-demo-app`, #2's old instance, running).
- EC2 `i-09744d897a25d257f` (`proba`, stopped).

## Moving to a new account

1. `cd terraform/bootstrap && terraform init && terraform apply` (creates the state bucket).
2. Change the backend bucket name in `terraform/providers.tf` (the only hardcoded account id).
3. Copy `envs/dev.tfvars.example` to `envs/dev.tfvars`, fill in secrets.
4. `cd terraform && terraform init && terraform apply -var-file=envs/dev.tfvars`.
5. Recreate the resources from the first table (run `scripts/deploy_predictor.sh`, upload `site/`).
