# Kezekshi: AI On-Call Engineer

An AI that watches your servers, reads the logs when something breaks and explains what went wrong, so nobody has to wake up at 3 AM.



## The problem

Small and medium companies move to the cloud but rarely have an on-call engineer. Monitoring tools say "CPU > 80%" and stop there. Someone still has to find the cause, usually at night and usually after customers have noticed.

## How it works

1. **Connect a server.** On the website you enter a server name and get one command. Run it on any Linux server and the Kezekshi agent is installed as a systemd service.
2. **Watch.** Every minute the agent sends CPU, memory, disk and new error lines from the logs. Emails, phone numbers and tokens are masked before sending.
3. **Detect.** CloudWatch alarms fire on four failure types: application errors, high CPU, full disk and a server that stopped responding.
4. **Explain.** EventBridge passes the alarm to a Lambda function. It collects recent logs and metrics, sends them to the Gemini API and gets back a short incident report: what happened, the likely root cause and the first steps to fix it.
5. **Notify.** The report goes to Telegram and email and appears on the dashboard. When the alarm returns to OK, the incident is closed automatically.

## Architecture

```
Customer server                         Demo app (EC2)
  kezekshi-agent                          Flask app + CloudWatch Agent
       |                                        |
       v                                        v
API Gateway /ingest -> Lambda          CloudWatch logs and metrics
  (metrics, error logs, alarms)                 |
       |                                        |
       +------------> CloudWatch alarms <-------+
                            |
                            v
                  EventBridge (ALARM and OK)
                            |
                            v
            Lambda incident-analyzer + Gemini API
                            |
       +--------------------+--------------------+
       v                    v                    v
DynamoDB (incidents)   Telegram bot         SNS (email)
       |
       v
API Gateway -> Lambda API -> Dashboard (S3 static website)
```

More details: [docs/architecture.md](docs/architecture.md)

## AWS services

EC2, VPC and Security Groups, Elastic IP, CloudWatch (metrics, logs, alarms), EventBridge, Lambda, API Gateway, DynamoDB, S3, SNS, IAM, SSM Session Manager and Parameter Store. All infrastructure is described in Terraform.

## Repository structure

```
terraform/   infrastructure as code (network, EC2, monitoring, Lambda, API)
src/         Lambda code, the demo app and the Kezekshi agent
scripts/     setup.sh (demo server), demo-up.sh and demo-down.sh
docs/        architecture and setup guide
```

## Quick start

**Connect a server.** Get a command on the website (or call `POST /register`) and run it on the server:

```bash
curl -sSL https://<agent-bucket>.s3.eu-north-1.amazonaws.com/install.sh | sudo bash -s -- --key <KEY> --name <SERVER_NAME>
```

The first alarms appear about a minute after the agent connects.

**Break the demo app.** The token is printed at the end of `scripts/setup.sh`:

```bash
curl -X POST -H "X-Demo-Token: <TOKEN>" http://<demo-ip>:8080/break/500
curl -X POST -H "X-Demo-Token: <TOKEN>" http://<demo-ip>:8080/break/cpu
curl -X POST -H "X-Demo-Token: <TOKEN>" http://<demo-ip>:8080/break/disk
curl -X POST -H "X-Demo-Token: <TOKEN>" http://<demo-ip>:8080/fix
```

Full guide: [docs/setup-guide.md](docs/setup-guide.md)

