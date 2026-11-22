# Setup guide

## Requirements

- AWS account and AWS CLI configured (`aws configure`)
- Terraform >= 1.10
- A Gemini API key
- Session Manager plugin for the AWS CLI (to log in to the EC2 instance without SSH)

Region: `eu-north-1` (Stockholm).

## State bucket (once per AWS account)

Terraform state is stored in S3 (`incident-demo-tfstate-<account id>`), so the whole team works with the same state. The bucket already exists in our account. Only for a new account:

```bash
cd terraform/bootstrap
terraform init && terraform apply
```

## Deploy

```bash
cp terraform/envs/dev.tfvars.example terraform/envs/dev.tfvars
export TF_VAR_gemini_api_key="your-key"   # never commit this
./scripts/demo-up.sh
```

After deploy, `terraform output` shows the instance id, app URL and DynamoDB table name.

Log in to the instance:

```bash
aws ssm start-session --target <instance_id> --region eu-north-1
```

## Destroy

```bash
./scripts/demo-down.sh
```

## Notes

- Do not create resources by hand in the console. Everything must be in Terraform, so the stack can be rebuilt at any time.
