# Kezekshi: AI On-Call Engineer
An AI that watches your servers, reads the logs when something breaks and explains what went wrong, so nobody has to wake up at 3 AM.


# The problem
Small and medium companies move to the cloud but rarely have an on-call engineer. Monitoring tools say "CPU > 80%" and stop there. Someone still has to find the cause, usually at night and usually after customers have noticed.

# How it works
Connect a server. On the website you enter a server name and get one command. Run it on any Linux server and the Kezekshi agent is installed as a systemd service.
Watch. Every minute the agent sends CPU, memory, disk and new error lines from the logs. Emails, phone numbers and tokens are masked before sending.
Detect. CloudWatch alarms fire on four failure types: application errors, high CPU, full disk and a server that stopped responding.
Explain. EventBridge passes the alarm to a Lambda function. It collects recent logs and metrics, sends them to the Gemini API and gets back a short incident report: what happened, the likely root cause and the first steps to fix it.
Notify. The report goes to Telegram and email and appears on the dashboard. When the alarm returns to OK, the incident is closed automatically.

# AWS services
EC2, VPC and Security Groups, Elastic IP, CloudWatch (metrics, logs, alarms), EventBridge, Lambda, API Gateway, DynamoDB, S3, SNS, IAM, SSM Session Manager and Parameter Store. All infrastructure is described in Terraform.

# Repository structure
terraform/   infrastructure as code (network, EC2, monitoring, Lambda, API)
src/         Lambda code, the demo app and the Kezekshi agent
scripts/     setup.sh (demo server), demo-up.sh and demo-down.sh
docs/        architecture and setup guide
