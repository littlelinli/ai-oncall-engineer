# Ресурсы, которые сначала создали руками в консоли, а потом описали в Terraform.
# import берёт их под управление Terraform как есть, ничего не пересоздавая.
#
# НА НОВОМ АККАУНТЕ ЭТОТ ФАЙЛ НУЖНО УДАЛИТЬ: там этих ресурсов ещё нет,
# и Terraform просто создаст их с нуля.

# ML-агент
import {
  to = aws_dynamodb_table.predictions
  id = "kezekshi-predictions"
}
import {
  to = aws_iam_role.predictor
  id = "kezekshi-predictor-role"
}
import {
  to = aws_iam_role_policy_attachment.predictor_logs
  id = "kezekshi-predictor-role/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
import {
  to = aws_iam_role_policy.predictor
  id = "kezekshi-predictor-role:predictor-access"
}
import {
  to = aws_cloudwatch_log_group.predictor
  id = "/aws/lambda/kezekshi-predictor"
}
import {
  to = aws_lambda_function.predictor
  id = "kezekshi-predictor"
}
import {
  to = aws_cloudwatch_event_rule.predictor
  id = "kezekshi-predictor-every-5-min"
}
import {
  to = aws_cloudwatch_event_target.predictor
  id = "kezekshi-predictor-every-5-min/predictor"
}
import {
  to = aws_lambda_permission.predictor_events
  id = "kezekshi-predictor/events-invoke"
}

# Датасет
import {
  to = aws_s3_bucket.dataset
  id = "kezekshi-dataset-788383723111"
}
import {
  to = aws_s3_bucket_public_access_block.dataset
  id = "kezekshi-dataset-788383723111"
}
import {
  to = aws_iam_role_policy.dataset_upload
  id = "incident-demo-app:shopsim-dataset-upload"
}

# API для сайта (роль заменяется новой, созданной Terraform)
import {
  to = aws_cloudwatch_log_group.api
  id = "/aws/lambda/kezekshi-api"
}
import {
  to = aws_lambda_function.api
  id = "kezekshi-api"
}
import {
  to = aws_lambda_function_url.api
  id = "kezekshi-api"
}

# Сайт
import {
  to = aws_s3_bucket.site
  id = "kezekshi-dashboard-alikhan"
}
import {
  to = aws_s3_bucket_website_configuration.site
  id = "kezekshi-dashboard-alikhan"
}
import {
  to = aws_s3_bucket_public_access_block.site
  id = "kezekshi-dashboard-alikhan"
}
import {
  to = aws_s3_bucket_policy.site
  id = "kezekshi-dashboard-alikhan"
}
