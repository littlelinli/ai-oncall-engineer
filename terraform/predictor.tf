# ML-агент kezekshi-predictor: раз в 5 минут прогнозирует метрики серверов,
# пишет прогноз в DynamoDB и CloudWatch, предупреждает в Telegram заранее.
# Code: src/lambda/predictor/ (lambda_function.py, model.py)

data "archive_file" "predictor" {
  type        = "zip"
  source_dir  = "${path.module}/../src/lambda/predictor"
  output_path = "${path.module}/build/predictor.zip"
  excludes    = ["__pycache__"]
}

resource "aws_dynamodb_table" "predictions" {
  name         = "kezekshi-predictions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "server_id"
  range_key    = "ts"

  attribute {
    name = "server_id"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_iam_role" "predictor" {
  name               = "kezekshi-predictor-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "predictor_logs" {
  role       = aws_iam_role.predictor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "predictor" {
  # GetMetricData, ListMetrics and PutMetricData have no resource-level permissions
  statement {
    actions   = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "cloudwatch:PutMetricData"]
    resources = ["*"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.predictions.arn]
  }
  statement {
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.servers.arn]
  }
  # neural network forecast (ml.tf)
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.ml.arn]
  }
  # owner's Telegram chat (recipients.py)
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.users.arn]
  }
}

resource "aws_iam_role_policy" "predictor" {
  name   = "predictor-access"
  role   = aws_iam_role.predictor.id
  policy = data.aws_iam_policy_document.predictor.json
}

resource "aws_cloudwatch_log_group" "predictor" {
  name              = "/aws/lambda/kezekshi-predictor"
  retention_in_days = 14
}

resource "aws_lambda_function" "predictor" {
  function_name    = "kezekshi-predictor"
  role             = aws_iam_role.predictor.arn
  runtime          = "python3.12"
  handler          = "lambda_function.handler"
  filename         = data.archive_file.predictor.output_path
  source_code_hash = data.archive_file.predictor.output_base64sha256
  memory_size      = 256
  timeout          = 120

  environment {
    variables = {
      PREDICTIONS_TABLE  = aws_dynamodb_table.predictions.name
      SERVERS_TABLE      = aws_dynamodb_table.servers.name
      METRICS_NAMESPACE  = var.servers_metrics_namespace
      FORECAST_NAMESPACE = "${var.servers_metrics_namespace}/Forecast"
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
      TELEGRAM_CHAT_ID   = var.telegram_chat_id
      USERS_TABLE        = aws_dynamodb_table.users.name
      ML_FUNCTION        = aws_lambda_function.ml.function_name
    }
  }

  depends_on = [aws_iam_role_policy.predictor, aws_cloudwatch_log_group.predictor]
}

resource "aws_cloudwatch_event_rule" "predictor" {
  name                = "kezekshi-predictor-every-5-min"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "predictor" {
  rule      = aws_cloudwatch_event_rule.predictor.name
  target_id = "predictor"
  arn       = aws_lambda_function.predictor.arn
}

resource "aws_lambda_permission" "predictor_events" {
  statement_id  = "events-invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.predictor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.predictor.arn
}
