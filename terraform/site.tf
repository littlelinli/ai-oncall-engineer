# Сайт Kezekshi: статический сайт в S3 (src/dashboard).
# Адреса API и /register подставляются в assets/config.js автоматически,
# поэтому на новом аккаунте сайт сразу смотрит на новые адреса.

locals {
  site_dir   = "${path.module}/../src/dashboard"
  site_files = [for f in fileset(local.site_dir, "**") : f if f != "README.md"]
  site_types = {
    html = "text/html; charset=utf-8"
    css  = "text/css; charset=utf-8"
    js   = "application/javascript; charset=utf-8"
    svg  = "image/svg+xml"
    png  = "image/png"
    ico  = "image/x-icon"
    json = "application/json"
  }
  site_config = replace(
    replace(file("${local.site_dir}/assets/config.js"),
      "/API_URL: \"[^\"]*\"/", "API_URL: \"${trimsuffix(aws_lambda_function_url.api.function_url, "/")}\""),
    "/REGISTER_URL: \"[^\"]*\"/", "REGISTER_URL: \"${local.register_url}\"")
}

resource "aws_s3_bucket" "site" {
  bucket = var.site_bucket_name
}

resource "aws_s3_bucket_website_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  index_document {
    suffix = "index.html"
  }
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

data "aws_iam_policy_document" "site_public" {
  statement {
    sid       = "PublicRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.site.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket     = aws_s3_bucket.site.id
  policy     = data.aws_iam_policy_document.site_public.json
  depends_on = [aws_s3_bucket_public_access_block.site]
}

resource "aws_s3_object" "site" {
  for_each      = toset(local.site_files)
  bucket        = aws_s3_bucket.site.id
  key           = each.value
  content       = each.value == "assets/config.js" ? local.site_config : null
  source        = each.value == "assets/config.js" ? null : "${local.site_dir}/${each.value}"
  etag          = each.value == "assets/config.js" ? md5(local.site_config) : filemd5("${local.site_dir}/${each.value}")
  content_type  = lookup(local.site_types, reverse(split(".", each.value))[0], "application/octet-stream")
  cache_control = "no-cache"
}
