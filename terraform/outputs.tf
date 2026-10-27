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
  description = "Public IP of the demo app"
  value       = one(aws_instance.app[*].public_ip)
}

output "app_url" {
  description = "Demo app URL"
  value       = var.enable_ec2 ? "http://${aws_instance.app[0].public_ip}:${var.app_port}" : null
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

output "incidents_table_arn" {
  description = "DynamoDB table ARN (for Lambda IAM policies)"
  value       = aws_dynamodb_table.incidents.arn
}
