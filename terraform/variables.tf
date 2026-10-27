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

variable "incident_lambda_arn" {
  description = "ARN of the Lambda that analyzes an incident and sends it to Telegram"
  type        = string
  default     = "arn:aws:lambda:eu-north-1:788383723111:function:incident-analyzer"
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
