variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "eu-north-1"
}

variable "project_name" {
  description = "Project name used in resource names and tags"
  type        = string
  default     = "ai-oncall-engineer"
}

variable "environment" {
  description = "Environment name (dev or prod)"
  type        = string
  default     = "dev"
}

variable "enable_ec2" {
  description = "Create the EC2 demo app (the only part that costs money while running)"
  type        = bool
  default     = true
}

variable "instance_type" {
  description = "EC2 instance type for the demo app"
  type        = string
  default     = "t3.micro"
}

variable "app_port" {
  description = "Port the demo app listens on"
  type        = number
  default     = 8080
}

# Secrets: set them in envs/dev.tfvars (not in git) or as TF_VAR_* env variables
variable "gemini_api_key" {
  description = "Gemini API key for the incident Lambda"
  type        = string
  sensitive   = true
}

variable "telegram_bot_token" {
  description = "Telegram bot token for incident messages"
  type        = string
  sensitive   = true
}

variable "telegram_chat_id" {
  description = "Telegram chat that receives incident messages"
  type        = string
}

variable "alert_emails" {
  description = "Emails subscribed to incident alerts (SNS)"
  type        = list(string)
  default     = []
}

variable "alarm_prefix" {
  description = "Alarm name prefix; the EventBridge rule only catches these alarms"
  type        = string
  default     = "ai-oncall-engineer-"
}

variable "root_disk_device" {
  description = "Root disk name in the CWAgent metric (nvme0n1p1 for t3/t4g)"
  type        = string
  default     = "nvme0n1p1"
}

variable "app_allowed_cidr" {
  description = "Who can open the demo app in a browser"
  type        = string
  default     = "0.0.0.0/0"
}

variable "site_allowed_origins" {
  description = "Sites allowed to call /register from the browser (CORS)"
  type        = list(string)
  default     = ["*"]
}

variable "servers_metrics_namespace" {
  description = "CloudWatch namespace for metrics sent by agents on connected servers"
  type        = string
  default     = "Kezekshi"
}

variable "app_running" {
  description = "Demo server state: true = running, false = stopped (keeps disk, agent and IP; see scripts/demo-down)"
  type        = bool
  default     = true
}
