variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
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
  default     = false
}
