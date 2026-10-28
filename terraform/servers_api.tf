# Connecting customer servers: site -> POST /register (key + install command),
# agent on the server -> POST /ingest every minute.
# Code: src/lambda/register, src/lambda/ingest

locals {
  register_url = "${aws_apigatewayv2_api.servers.api_endpoint}/register"
  ingest_url   = "${aws_apigatewayv2_api.servers.api_endpoint}/ingest"
}

# One item per connected server. Key = sha256 of the agent key (the key itself is not stored)
resource "aws_dynamodb_table" "servers" {
  name         = "incident-demo-servers"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "key_hash"

  attribute {
    name = "key_hash"
    type = "S"
  }

  # /register sets ttl = now + 1 day, the first /ingest removes it
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# Agent errors (filled by the /ingest Lambda, one log stream per server)
resource "aws_cloudwatch_log_group" "servers" {
  name              = "/kezekshi/servers"
  retention_in_days = 7
}

# ---------- /register ----------

data "archive_file" "register" {
  type        = "zip"
  source_dir  = "${path.module}/../src/lambda/register"
  output_path = "${path.module}/build/register.zip"
}

resource "aws_iam_role" "register" {
  name               = "incident-demo-register"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "register" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.register.arn}:*"]
  }

  statement {
    sid       = "AddServer"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.servers.arn]
  }
}

resource "aws_iam_role_policy" "register" {
  name   = "register"
  role   = aws_iam_role.register.id
  policy = data.aws_iam_policy_document.register.json
}

resource "aws_cloudwatch_log_group" "register" {
  name              = "/aws/lambda/kezekshi-register"
  retention_in_days = 14
}

resource "aws_lambda_function" "register" {
  function_name    = "kezekshi-register"
  role             = aws_iam_role.register.arn
  runtime          = "python3.14"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.register.output_path
  source_code_hash = data.archive_file.register.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      TABLE_NAME  = aws_dynamodb_table.servers.name
      INSTALL_URL = local.agent_install_url
      INGEST_URL  = local.ingest_url
    }
  }

  depends_on = [aws_iam_role_policy.register, aws_cloudwatch_log_group.register]
}

# ---------- /ingest ----------

data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.module}/../src/lambda/ingest"
  output_path = "${path.module}/build/ingest.zip"
}

resource "aws_iam_role" "ingest" {
  name               = "incident-demo-ingest"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "ingest" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ingest.arn}:*"]
  }

  statement {
    sid       = "CheckKeyAndMarkOnline"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.servers.arn]
  }

  # PutMetricData has no resource-level permissions, so it is limited by namespace
  statement {
    sid       = "ServerMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.servers_metrics_namespace]
    }
  }

  statement {
    sid       = "ServerErrors"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.servers.arn}:*"]
  }

  # Alarms for a server are created on its first /ingest (with tags, hence TagResource).
  # Only the srv- names: the demo server alarms stay out of reach. The prefix routes them to incident-analyzer
  statement {
    sid       = "ServerAlarms"
    actions   = ["cloudwatch:PutMetricAlarm", "cloudwatch:TagResource"]
    resources = ["arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:${var.alarm_prefix}srv-*"]
  }
}

resource "aws_iam_role_policy" "ingest" {
  name   = "ingest"
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest.json
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/kezekshi-ingest"
  retention_in_days = 14
}

resource "aws_lambda_function" "ingest" {
  function_name    = "kezekshi-ingest"
  role             = aws_iam_role.ingest.arn
  runtime          = "python3.14"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      TABLE_NAME        = aws_dynamodb_table.servers.name
      METRICS_NAMESPACE = var.servers_metrics_namespace
      SERVERS_LOG_GROUP = aws_cloudwatch_log_group.servers.name
      ALARM_PREFIX      = var.alarm_prefix
    }
  }

  depends_on = [aws_iam_role_policy.ingest, aws_cloudwatch_log_group.ingest]
}

# ---------- API Gateway (HTTP API) ----------

resource "aws_apigatewayv2_api" "servers" {
  name          = "kezekshi-servers-api"
  protocol_type = "HTTP"

  # The site calls /register from the browser
  cors_configuration {
    allow_origins = var.site_allowed_origins
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["content-type", "x-kezekshi-key"]
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.servers.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = 20
    throttling_burst_limit = 40
  }

  # /register is public, so it gets a tighter limit
  route_settings {
    route_key              = aws_apigatewayv2_route.register.route_key
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }
}

resource "aws_apigatewayv2_integration" "register" {
  api_id                 = aws_apigatewayv2_api.servers.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.register.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "register" {
  api_id    = aws_apigatewayv2_api.servers.id
  route_key = "POST /register"
  target    = "integrations/${aws_apigatewayv2_integration.register.id}"
}

resource "aws_lambda_permission" "register_api" {
  statement_id  = "AllowApiGatewayRegister"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.register.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.servers.execution_arn}/*/*/register"
}

resource "aws_apigatewayv2_integration" "ingest" {
  api_id                 = aws_apigatewayv2_api.servers.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.ingest.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "ingest" {
  api_id    = aws_apigatewayv2_api.servers.id
  route_key = "POST /ingest"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_lambda_permission" "ingest_api" {
  statement_id  = "AllowApiGatewayIngest"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.servers.execution_arn}/*/*/ingest"
}
