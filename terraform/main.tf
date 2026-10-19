terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# Modules will be added step by step:
# module "network"    { source = "./modules/network" }
# module "app"        { source = "./modules/app" }
# module "monitoring" { source = "./modules/monitoring" }
# module "processor"  { source = "./modules/processor" }
