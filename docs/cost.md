# Cost Estimate

Region eu-north-1 (Stockholm), AWS on-demand prices as of 2026-10-28 (from the AWS Pricing API).
Month = 730 hours, everything runs 24/7. Paid account with no 12-month Free Tier; the always-free
Lambda limits (1M requests and 400,000 GB-s per month) apply to every account.
Gemini API and Telegram are not included (not AWS).

## Summary

Cost = **platform ≈ $13.8** (fixed) + **≈ $2.5 per connected client server**.

| Connected servers | 0 | 1 | 10 | 50 | 100 |
|---|---|---|---|---|---|
| Per month | $13.8 | $16.3 | $38.8 | $138.8 | $263.8 |

The platform cost is almost entirely the demo server. API, Lambda, tables and S3 cost cents
at our load: they are billed per request, not per hour.

## Platform (24/7)

| Resource | Price | Per month |
|---|---|---|
| Demo server EC2 t3.micro | $0.0108 / hour | $7.88 |
| Demo server Elastic IP | $0.005 / hour | $3.65 |
| EBS gp3 disk, 8 GB | $0.0836 / GB-month | $0.67 |
| Demo server metrics: CWAgent cpu, mem, disk + DemoApp/AppErrors | 4 × $0.30 | $1.20 |
| Alarms app-errors, cpu-high, disk-high | 3 × $0.10 | $0.30 |
| CloudWatch Logs (app and Lambda, < 0.1 GB) | $0.54 / GB | < $0.05 |
| Lambda, API Gateway, DynamoDB, S3, SNS with no clients | per request | < $0.05 |
| **Total** | | **≈ $13.8** |

## One connected client server

The agent sends 1 request per minute = 43,800 requests per month.

| Resource | Calculation | Per month |
|---|---|---|
| Metrics CpuPercent, MemPercent, DiskPercent, ErrorCount, Heartbeat | 5 × $0.30 | $1.50 |
| PutMetricData requests (1 call for all 5 metrics) | 43,800 × $0.01 / 1000 | $0.44 |
| Alarms per server: cpu-high, disk-high, app-errors, server-down | 4 × $0.10 | $0.40 |
| API Gateway HTTP API | 43,800 × $1.06 / million | $0.05 |
| DynamoDB: key lookup + online status | 43,800 × ($0.13 + $0.67) / million | $0.04 |
| Lambda /ingest (128 MB, ~100 ms) | free up to ~22 servers, then | $0.02 |
| /ingest logs and server errors | ~30 MB × $0.54 / GB | $0.02 |
| **Total** | | **≈ $2.5** |

Incident analysis (incident-analyzer) runs only when an alarm fires and costs a fraction of a cent;
the main cost per incident is the Gemini request.

## How to lower the per-server cost

Over 90% of the per-server cost is CloudWatch: metrics ($1.50), PutMetricData requests ($0.44) and alarms ($0.40).

- Send all 5 metrics in **one** PutMetricData call (already done): 5 separate calls would cost
  $2.19 instead of $0.44.
- With many clients, thresholds (CPU > 80% etc.) can be checked directly in the /ingest Lambda,
  keeping the latest values in DynamoDB. A server then costs ≈ $0.15 per month, but we lose
  CloudWatch graphs and alarms and have to write the alerting logic ourselves. For the MVP we keep CloudWatch.
- Metrics get cheaper on their own: after 10,000 metrics (~2,500 servers) the price drops from $0.30 to $0.10.

## The demo server can be turned off

`scripts/demo-down` stops the demo server (EC2 is not billed; disk, IP and everything else
stay), `scripts/demo-up` starts it again with the same IP. The platform then costs ≈ $4.6 per month
instead of $13.8. Client servers are not affected: API and Lambda always run.

## Price sources

| Service | Price |
|---|---|
| EC2 t3.micro Linux | $0.0108 / hour |
| Public IPv4 (idle or in use) | $0.005 / hour |
| EBS gp3 | $0.0836 / GB-month |
| CloudWatch: custom metric (first 10,000) | $0.30 / month |
| CloudWatch: standard alarm | $0.10 / month |
| CloudWatch: PutMetricData | $0.01 / 1000 requests |
| CloudWatch Logs: ingestion | $0.54 / GB |
| API Gateway HTTP API | $1.06 / million requests |
| DynamoDB on-demand: write / read | $0.67 / $0.1345 per million |
| Lambda: requests / compute | $0.20 per million / $0.0000166667 per GB-s |
