# AI On-Call Engineer

An AI that reads your cloud logs and explains what went wrong, so nobody has to wake up at 3 AM.

**Team:** Urazova A, Rudkovskaya A, Lilizov R, Kemelkhan A

## Idea

A demo web app runs on AWS. When something breaks (high CPU, many HTTP 500 errors), a CloudWatch alarm starts the system. A Lambda function collects the recent logs and metrics, sends them to the Gemini API, and gets back a short incident report: what happened, the likely root cause, and what to do. The report is saved and sent by email.

## Architecture

```
EC2 demo app -> CloudWatch (logs, metrics, alarm) -> EventBridge -> Lambda + Gemini
                                                                      |
                                                  DynamoDB (history) + S3 (reports) + SNS (email)
```

More details: [docs/architecture.md](docs/architecture.md)

## AWS services

EC2, VPC and Security Groups, CloudWatch, EventBridge, Lambda, DynamoDB, S3, SNS, IAM, SSM Parameter Store

## Repository structure

```
terraform/   infrastructure as code (main.tf, variables.tf, outputs.tf, envs/)
src/         Lambda code and the demo app
scripts/     demo-up.sh and demo-down.sh
docs/        architecture and setup guide
```

## Status

- [x] Idea and architecture design
- [ ] Network and EC2 (Terraform)
- [ ] CloudWatch alarms and EventBridge
- [ ] Lambda + Gemini integration
- [ ] DynamoDB, S3 and SNS
- [ ] Dashboard
- [ ] Cost analysis and final documentation

## Quick start

See [docs/setup-guide.md](docs/setup-guide.md).

## Cost

The project is designed to stay inside the AWS free tier. The whole stack can be created and destroyed with Terraform.
