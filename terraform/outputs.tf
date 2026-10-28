output "project_name" {
  description = "Project name"
  value       = var.project_name
}

output "environment" {
  description = "Current environment"
  value       = var.environment
}

output "instance_id" {
  description = "Demo app EC2 instance (connect with: aws ssm start-session --target <id>)"
  value       = one(aws_instance.app[*].id)
}

output "instance_public_ip" {
  description = "Public IP of the demo app (Elastic IP, does not change)"
  value       = aws_eip.app.public_ip
}

output "app_url" {
  description = "Demo app URL"
  value       = var.enable_ec2 ? "http://${aws_eip.app.public_ip}:${var.app_port}" : null
}

output "subnet_id" {
  description = "Public subnet"
  value       = aws_subnet.public.id
}

output "security_group_id" {
  description = "Security group of the demo app"
  value       = aws_security_group.app.id
}

output "ec2_role_name" {
  description = "IAM role of the demo app instance"
  value       = aws_iam_role.app.name
}

output "incidents_table_name" {
  description = "DynamoDB table with incident history"
  value       = aws_dynamodb_table.incidents.name
}

output "alarm_names" {
  description = "Alarms that send incidents to the Lambda"
  value = concat(
    [aws_cloudwatch_metric_alarm.app_errors.alarm_name],
    aws_cloudwatch_metric_alarm.cpu_high[*].alarm_name,
    aws_cloudwatch_metric_alarm.disk_high[*].alarm_name,
  )
}

output "incidents_table_arn" {
  description = "DynamoDB table ARN (for Lambda IAM policies)"
  value       = aws_dynamodb_table.incidents.arn
}

output "register_url" {
  description = "POST: site gets a server key and the install command (-> #4)"
  value       = local.register_url
}

output "ingest_url" {
  description = "POST: agent sends metrics, header X-Kezekshi-Key (-> #2, #3)"
  value       = local.ingest_url
}

output "agent_install_url" {
  description = "Public agent installer: curl -sSL <url> | sudo bash -s -- --key KEY --name NAME"
  value       = local.agent_install_url
}

output "servers_table_name" {
  description = "DynamoDB table with connected servers"
  value       = aws_dynamodb_table.servers.name
}
