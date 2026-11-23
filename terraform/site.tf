# Сайт Kezekshi: статический сайт в S3 (src/dashboard), снаружи CloudFront с HTTPS
# (вход по паролю без HTTPS делать нельзя).
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
  # адреса, которые сайт берёт из assets/config.js
  site_settings = {
    API_URL           = trimsuffix(aws_lambda_function_url.api.function_url, "/")
    REGISTER_URL      = local.register_url
    ACCOUNT_API_URL   = local.account_api_url
    COGNITO_REGION    = var.aws_region
    COGNITO_CLIENT_ID = aws_cognito_user_pool_client.site.id
  }
  site_config_raw = file("${local.site_dir}/assets/config.js")
  site_config = replace(replace(replace(replace(replace(local.site_config_raw,
    "/API_URL: \"[^\"]*\"/", "API_URL: \"${local.site_settings.API_URL}\""),
    "/REGISTER_URL: \"[^\"]*\"/", "REGISTER_URL: \"${local.site_settings.REGISTER_URL}\""),
    "/ACCOUNT_API_URL: \"[^\"]*\"/", "ACCOUNT_API_URL: \"${local.site_settings.ACCOUNT_API_URL}\""),
    "/COGNITO_REGION: \"[^\"]*\"/", "COGNITO_REGION: \"${local.site_settings.COGNITO_REGION}\""),
  "/COGNITO_CLIENT_ID: \"[^\"]*\"/", "COGNITO_CLIENT_ID: \"${local.site_settings.COGNITO_CLIENT_ID}\"")
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

# ---------- HTTPS: CloudFront перед сайтом ----------

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  comment             = "Kezekshi website"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"

  origin {
    origin_id   = "s3-website"
    domain_name = aws_s3_bucket_website_configuration.site.website_endpoint
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only" # S3 website endpoint умеет только http
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "s3-website"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # Managed-CachingDisabled: изменения сайта видны сразу, без сброса кэша
    cache_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
