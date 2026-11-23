# Аккаунты Kezekshi: вход на сайт (Amazon Cognito), личный кабинет и свой Telegram у каждого.
#
#   сайт -> Cognito (регистрация, вход по почте и паролю) -> JWT
#   сайт -> API Gateway /me/* и /register с JWT -> Lambda kezekshi-account / kezekshi-register
#   Telegram -> POST /telegram/{secret} -> Lambda kezekshi-telegram (привязка чата, команды бота)
# Code: src/lambda/account, src/lambda/telegram

locals {
  account_api_url = aws_apigatewayv2_api.servers.api_endpoint
  # секрет вебхука выводится из токена бота: отдельный провайдер для случайных строк не нужен
  telegram_webhook_secret = substr(sha256("kezekshi-webhook:${var.telegram_bot_token}"), 0, 40)
  telegram_webhook_url    = "${aws_apigatewayv2_api.servers.api_endpoint}/telegram/${local.telegram_webhook_secret}"
  account_routes = [
    "GET /me",
    "PATCH /me",
    "GET /me/servers",
    "DELETE /me/servers/{id}",
    "GET /me/servers/{id}/metrics",
    "GET /me/incidents",
    "POST /me/telegram",
    "DELETE /me/telegram",
  ]
}

# ---------- Cognito ----------

resource "aws_cognito_user_pool" "users" {
  name                     = "kezekshi-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "ACTIVE"

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_numbers                  = true
    require_uppercase                = false
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Kezekshi: код подтверждения / verification code"
    email_message        = "Ваш код подтверждения Kezekshi: {####}\n\nYour Kezekshi verification code: {####}"
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 5
      max_length = 254
    }
  }
}

resource "aws_cognito_user_pool_client" "site" {
  name         = "kezekshi-site"
  user_pool_id = aws_cognito_user_pool.users.id

  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"

  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 30
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}

# ---------- пользователи: язык, Telegram, одноразовые ссылки привязки ----------

resource "aws_dynamodb_table" "users" {
  name         = "kezekshi-users"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id" # sub из Cognito; "link#<код>" для ссылок привязки Telegram

  attribute {
    name = "user_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ---------- проверка входа на API Gateway ----------

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.servers.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://${aws_cognito_user_pool.users.endpoint}"
    audience = [aws_cognito_user_pool_client.site.id]
  }
}

# ---------- личный кабинет ----------

data "archive_file" "account" {
  type        = "zip"
  source_dir  = "${path.module}/../src/lambda/account"
  output_path = "${path.module}/build/account.zip"
  excludes    = ["__pycache__"]
}

resource "aws_iam_role" "account" {
  name               = "incident-demo-account"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "account" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.account.arn}:*"]
  }
  statement {
    sid       = "Servers"
    actions   = ["dynamodb:Scan", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.servers.arn]
  }
  statement {
    sid       = "Users"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.users.arn]
  }
  statement {
    sid       = "ReadIncidentsAndForecasts"
    actions   = ["dynamodb:Scan", "dynamodb:Query"]
    resources = [aws_dynamodb_table.incidents.arn, aws_dynamodb_table.predictions.arn]
  }
  # GetMetricData has no resource-level permissions
  statement {
    sid       = "ReadMetrics"
    actions   = ["cloudwatch:GetMetricData"]
    resources = ["*"]
  }
  statement {
    sid       = "RemoveServerAlarms"
    actions   = ["cloudwatch:DeleteAlarms"]
    resources = ["arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:${var.alarm_prefix}srv-*"]
  }
}

resource "aws_iam_role_policy" "account" {
  name   = "account"
  role   = aws_iam_role.account.id
  policy = data.aws_iam_policy_document.account.json
}

resource "aws_cloudwatch_log_group" "account" {
  name              = "/aws/lambda/kezekshi-account"
  retention_in_days = 14
}

resource "aws_lambda_function" "account" {
  function_name    = "kezekshi-account"
  role             = aws_iam_role.account.arn
  runtime          = "python3.14"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.account.output_path
  source_code_hash = data.archive_file.account.output_base64sha256
  memory_size      = 256
  timeout          = 15

  environment {
    variables = {
      SERVERS_TABLE      = aws_dynamodb_table.servers.name
      USERS_TABLE        = aws_dynamodb_table.users.name
      INCIDENTS_TABLE    = aws_dynamodb_table.incidents.name
      PREDICTIONS_TABLE  = aws_dynamodb_table.predictions.name
      METRICS_NAMESPACE  = var.servers_metrics_namespace
      ALARM_PREFIX       = var.alarm_prefix
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
    }
  }

  depends_on = [aws_iam_role_policy.account, aws_cloudwatch_log_group.account]
}

resource "aws_apigatewayv2_integration" "account" {
  api_id                 = aws_apigatewayv2_api.servers.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.account.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "account" {
  for_each           = toset(local.account_routes)
  api_id             = aws_apigatewayv2_api.servers.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.account.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_lambda_permission" "account_api" {
  statement_id  = "AllowApiGatewayAccount"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.account.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.servers.execution_arn}/*/*/me*"
}

# ---------- Telegram-бот ----------

data "archive_file" "telegram" {
  type        = "zip"
  source_dir  = "${path.module}/../src/lambda/telegram"
  output_path = "${path.module}/build/telegram.zip"
  excludes    = ["__pycache__"]
}

resource "aws_iam_role" "telegram" {
  name               = "incident-demo-telegram"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "telegram" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.telegram.arn}:*"]
  }
  statement {
    sid       = "Users"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.users.arn]
  }
  statement {
    sid       = "Servers"
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.servers.arn]
  }
}

resource "aws_iam_role_policy" "telegram" {
  name   = "telegram"
  role   = aws_iam_role.telegram.id
  policy = data.aws_iam_policy_document.telegram.json
}

resource "aws_cloudwatch_log_group" "telegram" {
  name              = "/aws/lambda/kezekshi-telegram"
  retention_in_days = 14
}

resource "aws_lambda_function" "telegram" {
  function_name    = "kezekshi-telegram"
  role             = aws_iam_role.telegram.arn
  runtime          = "python3.14"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.telegram.output_path
  source_code_hash = data.archive_file.telegram.output_base64sha256
  memory_size      = 128
  timeout          = 15

  environment {
    variables = {
      USERS_TABLE        = aws_dynamodb_table.users.name
      SERVERS_TABLE      = aws_dynamodb_table.servers.name
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
      WEBHOOK_SECRET     = local.telegram_webhook_secret
    }
  }

  depends_on = [aws_iam_role_policy.telegram, aws_cloudwatch_log_group.telegram]
}

resource "aws_apigatewayv2_integration" "telegram" {
  api_id                 = aws_apigatewayv2_api.servers.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.telegram.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "telegram" {
  api_id    = aws_apigatewayv2_api.servers.id
  route_key = "POST /telegram/{secret}"
  target    = "integrations/${aws_apigatewayv2_integration.telegram.id}"
}

resource "aws_lambda_permission" "telegram_api" {
  statement_id  = "AllowApiGatewayTelegram"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.telegram.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.servers.execution_arn}/*/*/telegram/*"
}

# Говорим Telegram, куда слать сообщения боту. Запускается заново, если поменялся адрес.
resource "terraform_data" "telegram_webhook" {
  triggers_replace = [sha256(local.telegram_webhook_url)]

  provisioner "local-exec" {
    command = "curl -sS -o /dev/null -w 'setWebhook: HTTP %%{http_code}\\n' https://api.telegram.org/bot$TG_TOKEN/setWebhook -d url=$TG_URL -d secret_token=$TG_SECRET -d 'allowed_updates=[\"message\"]'"
    environment = {
      TG_TOKEN  = var.telegram_bot_token
      TG_URL    = local.telegram_webhook_url
      TG_SECRET = local.telegram_webhook_secret
    }
  }

  depends_on = [aws_apigatewayv2_route.telegram, aws_lambda_permission.telegram_api]
}
