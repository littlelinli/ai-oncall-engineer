# Lambda kezekshi-api: публичный Function URL, отдаёт сайту инциденты демо-сервера (витрина без входа).
# Инциденты своих серверов пользователь получает через личный кабинет (accounts.tf).
# Code: src/lambda/api/lambda_function.py

data "archive_file" "api" {
  type        = "zip"
  source_file = "${path.module}/../src/lambda/api/lambda_function.py"
  output_path = "${path.module}/build/api.zip"
}

resource "aws_iam_role" "api" {
  name               = "incident-demo-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }

  statement {
    sid       = "ReadIncidents"
    actions   = ["dynamodb:Scan", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.incidents.arn]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/kezekshi-api"
  retention_in_days = 14
}

resource "aws_lambda_function" "api" {
  function_name    = "kezekshi-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.14"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      TABLE_NAME     = aws_dynamodb_table.incidents.name
      DEMO_SERVER_ID = var.demo_server_id # без входа сайт показывает только демо-сервер
    }
  }

  depends_on = [aws_iam_role_policy.api, aws_cloudwatch_log_group.api]
}

resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE"
}

# Публичный доступ к Function URL (сайт читает инциденты без авторизации)
resource "aws_lambda_permission" "api_url" {
  statement_id           = "kezekshi-api-public-url"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.api.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
