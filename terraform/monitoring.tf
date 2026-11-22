# Мониторинг демо-приложения: лог-группа, метрика ошибок, алармы и правило EventBridge.

resource "aws_cloudwatch_log_group" "app" {
  name              = "/demoapp/app"
  retention_in_days = 7
}

# Каждая строка лога с level = ERROR даёт +1 к метрике DemoApp/AppErrors
resource "aws_cloudwatch_log_metric_filter" "app_errors" {
  name           = "app-errors"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.level = \"ERROR\" }"

  metric_transformation {
    name          = "AppErrors"
    namespace     = "DemoApp"
    value         = "1"
    default_value = "0"
  }
}

# Алармы серверов (CPU, память, диск, ошибки, сервер пропал, приложение не отвечает,
# медленные ответы, атака на логин, шквал логов) создаёт Lambda kezekshi-ingest
# для каждого подключённого сервера, включая демо (src/lambda/ingest/lambda_function.py).
# Отдельные алармы демо-сервера через CloudWatch Agent убраны: они дублировали алармы агента.
# Метрика DemoApp/AppErrors остаётся, её читает incident-analyzer для статистики.

# Любой наш аларм перешёл в ALARM -> событие в Lambda
resource "aws_cloudwatch_event_rule" "alarm_to_lambda" {
  name        = "${var.alarm_prefix}alarm-state-change"
  description = "Send ALARM and OK state changes of ai-oncall-engineer alarms to the incident Lambda"

  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = [{ prefix = var.alarm_prefix }]
      state     = { value = ["ALARM", "OK"] }
    }
  })
}

resource "aws_cloudwatch_event_target" "incident_lambda" {
  rule = aws_cloudwatch_event_rule.alarm_to_lambda.name
  arn  = aws_lambda_function.incident_analyzer.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeAlarmRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.incident_analyzer.arn
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarm_to_lambda.arn
}
