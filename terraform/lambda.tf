# Lambda incident-analyzer: alarm -> logs and metrics -> rule-based explanation -> DynamoDB, Telegram, SNS email.
# Code: src/lambda/incident_processor/ (lambda_function.py, explain.py). No external AI API.

data "archive_file" "incident_analyzer" {
  type        = "zip"
  source_dir  = "${path.module}/../src/lambda/incident_processor"
  output_path = "${path.module}/build/incident_analyzer.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "incident_analyzer" {
  name               = "incident-demo-incident-analyzer"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "incident_analyzer" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.incident_analyzer.arn}:*"]
  }

  statement {
    sid       = "ReadAppLogs"
    actions   = ["logs:FilterLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*", "${aws_cloudwatch_log_group.servers.arn}:*"]
  }

  # Auto-fix: run the fixed /fix command on the demo server only
  statement {
    sid       = "AutoFixDemoServer"
    actions   = ["ssm:SendCommand"]
    resources = concat(aws_instance.app[*].arn, ["arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript"])
  }

  # GetCommandInvocation does not support resource-level permissions
  statement {
    sid       = "AutoFixResult"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }

  # GetMetricStatistics and ListMetrics do not support resource-level permissions
  statement {
    sid       = "ReadMetrics"
    actions   = ["cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics"]
    resources = ["*"]
  }

  # Put: new incident; Update: auto-fix result, grouping, resolve; Scan: open incident and history
  statement {
    sid       = "WriteIncidents"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.incidents.arn]
  }

  # Cause classifier (neural network, ml.tf)
  statement {
    sid       = "MlCause"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.ml.arn, aws_lambda_function.llm.arn]
  }

  # Who gets the message: owner of the server and his Telegram chat (recipients.py)
  statement {
    sid       = "FindOwner"
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.servers.arn]
  }
  statement {
    sid       = "OwnerTelegram"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.users.arn]
  }

  # "ML-агент предупреждал об этом за N мин": warning records of kezekshi-predictor
  statement {
    sid       = "ReadForecastWarnings"
    actions   = ["dynamodb:GetItem"]
    resources = ["arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/kezekshi-predictions"]
  }

  statement {
    sid       = "EmailAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.incident_alerts.arn, aws_sns_topic.user_alerts.arn]
  }
}

resource "aws_iam_role_policy" "incident_analyzer" {
  name   = "incident-analyzer"
  role   = aws_iam_role.incident_analyzer.id
  policy = data.aws_iam_policy_document.incident_analyzer.json
}

resource "aws_cloudwatch_log_group" "incident_analyzer" {
  name              = "/aws/lambda/incident-analyzer"
  retention_in_days = 14
}

resource "aws_sns_topic" "incident_alerts" {
  name = "incident-alerts"
}

# Each address gets a confirmation email and must click the link once
resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.alert_emails)

  topic_arn = aws_sns_topic.incident_alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_lambda_function" "incident_analyzer" {
  function_name    = "incident-analyzer"
  role             = aws_iam_role.incident_analyzer.arn
  runtime          = "python3.14"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.incident_analyzer.output_path
  source_code_hash = data.archive_file.incident_analyzer.output_base64sha256
  memory_size      = 128
  timeout          = 180

  environment {
    variables = {
      INSTANCE_ID        = try(aws_instance.app[0].id, "unknown")
      TABLE_NAME         = aws_dynamodb_table.incidents.name
      SNS_TOPIC_ARN      = aws_sns_topic.incident_alerts.arn
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
      TELEGRAM_CHAT_ID   = var.telegram_chat_id
      SERVERS_LOG_GROUP  = aws_cloudwatch_log_group.servers.name
      METRICS_NAMESPACE  = var.servers_metrics_namespace
      # автопочинка выключена: /fix обрывает проблемы симулятора и портит метки датасета
      AUTO_FIX           = var.auto_fix ? "true" : "false"
      PREDICTIONS_TABLE  = "kezekshi-predictions"
      DEMO_SERVER_ID     = var.demo_server_id
      SERVERS_TABLE         = aws_dynamodb_table.servers.name
      USERS_TABLE           = aws_dynamodb_table.users.name
      USER_ALERTS_TOPIC_ARN = aws_sns_topic.user_alerts.arn
      ML_FUNCTION           = aws_lambda_function.ml.function_name
      LLM_FUNCTION          = aws_lambda_function.llm.function_name
      SITE_URL              = "https://${aws_cloudfront_distribution.site.domain_name}"
    }
  }

  depends_on = [aws_iam_role_policy.incident_analyzer, aws_cloudwatch_log_group.incident_analyzer]
}
