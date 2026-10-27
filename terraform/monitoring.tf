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

# Поломка /break/500
resource "aws_cloudwatch_metric_alarm" "app_errors" {
  alarm_name          = "${var.alarm_prefix}app-errors"
  alarm_description   = "Demo app returns HTTP 500 errors"
  namespace           = "DemoApp"
  metric_name         = "AppErrors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  depends_on = [aws_cloudwatch_log_metric_filter.app_errors]
}

# Поломка /break/cpu (метрика от CloudWatch Agent, раз в минуту)
resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  count = var.enable_ec2 ? 1 : 0

  alarm_name          = "${var.alarm_prefix}cpu-high"
  alarm_description   = "CPU above 80% on demo app server"
  namespace           = "CWAgent"
  metric_name         = "cpu_usage_active"
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.app[0].id
    cpu        = "cpu-total"
  }
}

# Поломка /break/disk
resource "aws_cloudwatch_metric_alarm" "disk_high" {
  count = var.enable_ec2 ? 1 : 0

  alarm_name          = "${var.alarm_prefix}disk-high"
  alarm_description   = "Root disk above 85% on demo app server"
  namespace           = "CWAgent"
  metric_name         = "disk_used_percent"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.app[0].id
    path       = "/"
    device     = var.root_disk_device
    fstype     = "xfs"
  }
}

# Любой наш аларм перешёл в ALARM -> событие в Lambda
resource "aws_cloudwatch_event_rule" "alarm_to_lambda" {
  name        = "${var.alarm_prefix}alarm-state-change"
  description = "Send ALARM state changes of ai-oncall-engineer alarms to the incident Lambda"

  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = [{ prefix = var.alarm_prefix }]
      state     = { value = ["ALARM"] }
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
