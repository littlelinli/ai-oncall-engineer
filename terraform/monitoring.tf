# Мониторинг: лог-группа демо-приложения, метрика ошибок и правило EventBridge для всех алармов.
# Положить рядом с main.tf. Переменные ниже можно переопределить в tfvars.

variable "incident_lambda_arn" {
  description = "ARN Lambda, которая разбирает инцидент и шлёт в Telegram"
  type        = string
  default     = "arn:aws:lambda:eu-north-1:788383723111:function:incident-analyzer"
}

variable "alarm_prefix" {
  description = "Префикс имён алармов; правило EventBridge ловит только их"
  type        = string
  default     = "ai-oncall-engineer-"
}

# Лог-группа уже создана руками, поэтому импортируем её, а не создаём заново.
import {
  to = aws_cloudwatch_log_group.app
  id = "/demoapp/app"
}

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
# для каждого подключённого сервера, см. src/lambda/ingest/kezekshi_ingest.py.
# Метрика DemoApp/AppErrors остаётся: её читает incident-analyzer для статистики.

# Любой наш аларм перешёл в ALARM -> событие в Lambda
resource "aws_cloudwatch_event_rule" "alarm_to_lambda" {
  name        = "${var.alarm_prefix}alarm-state-change"
  description = "Send ALARM and OK state changes of ai-oncall-engineer alarms to the incident Lambda"

  # ALARM = инцидент начался, OK = починилось (Lambda закрывает инцидент в DynamoDB)
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
  arn  = var.incident_lambda_arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeAlarmRule"
  action        = "lambda:InvokeFunction"
  function_name = var.incident_lambda_arn
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarm_to_lambda.arn
}
