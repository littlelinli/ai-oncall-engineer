# Architecture

## Flow

```mermaid
flowchart LR
    A[EC2 demo app] -->|logs and metrics| B[CloudWatch]
    B -->|alarm| C[EventBridge]
    C --> D[Lambda]
    D -->|logs| E[Gemini API]
    E -->|analysis| D
    D --> F[(DynamoDB)]
    D --> G[(S3 reports)]
    D --> H[SNS email]
```

## Services

| Service | Role |
|---|---|
| EC2 + VPC + Security Group | Runs the demo app in a private network |
| CloudWatch | Logs, metrics, alarms, dashboard |
| EventBridge | Connects the alarm to Lambda |
| Lambda | Collects data and calls Gemini |
| DynamoDB + S3 | Incident history and full reports |
| SNS | Email alerts |
| IAM + SSM | Least-privilege roles, safe API key storage |

## Security (draft)

- Each Lambda has its own IAM role with only the permissions it needs.
- The Gemini API key is stored in SSM Parameter Store, never in Git.
- S3 buckets are private.

## TODO

- Data flow diagram
- Sequence diagram
- Cost analysis
