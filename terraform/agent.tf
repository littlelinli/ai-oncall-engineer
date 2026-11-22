# Public link to the agent installer:
#   curl -sSL <agent_install_url> | sudo bash -s -- --key KEY --name NAME
# Script: src/agent/install.sh (the agent itself is embedded in it).

locals {
  agent_install_url = "https://${aws_s3_bucket.agent.bucket_regional_domain_name}/install.sh"

  # Bake the /ingest address in, so the command does not need --url
  install_sh_source = file("${path.module}/../src/agent/install.sh")
  install_sh_marker = "$${KEZEKSHI_DEFAULT_INGEST_URL:-}"
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "agent" {
  bucket = "kezekshi-agent-${data.aws_caller_identity.current.account_id}"
}

# Public read through the bucket policy only; ACLs stay blocked
resource "aws_s3_bucket_public_access_block" "agent" {
  bucket                  = aws_s3_bucket.agent.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

data "aws_iam_policy_document" "agent_bucket" {
  statement {
    sid       = "PublicReadInstaller"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.agent.arn}/install.sh"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "agent" {
  bucket = aws_s3_bucket.agent.id
  policy = data.aws_iam_policy_document.agent_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.agent]
}

resource "aws_s3_object" "install_sh" {
  bucket        = aws_s3_bucket.agent.id
  key           = "install.sh"
  content       = replace(local.install_sh_source, local.install_sh_marker, "$${KEZEKSHI_DEFAULT_INGEST_URL:-${local.ingest_url}}")
  content_type  = "text/x-shellscript; charset=utf-8"
  cache_control = "no-cache"

  lifecycle {
    precondition {
      condition     = strcontains(local.install_sh_source, local.install_sh_marker)
      error_message = "src/agent/install.sh no longer contains URL=\"$${KEZEKSHI_DEFAULT_INGEST_URL:-}\"; update agent.tf."
    }
  }
}
