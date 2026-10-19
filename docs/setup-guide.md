# Setup guide

## Requirements

- AWS account and AWS CLI configured (`aws configure`)
- Terraform >= 1.5
- A Gemini API key

## Deploy

```bash
cp terraform/envs/dev.tfvars.example terraform/envs/dev.tfvars
export TF_VAR_gemini_api_key="your-key"   # never commit this
./scripts/demo-up.sh
```

## Destroy

```bash
./scripts/demo-down.sh
```

## Notes

- Do not create resources by hand in the console. Everything must be in Terraform, so the stack can be rebuilt at any time.
