# Lambda kezekshi-ml: нейросети (ONNX) из Docker-образа. Код и модели: src/ml (см. src/ml/README.md).
# Образ собирается и загружается в ECR при apply, когда меняется любой файл в src/ml (нужен Docker,
# в AWS CloudShell он есть). Прогноз и анализатор вызывают её, а если модели нет, работают сами.

locals {
  ml_dir   = "${path.module}/../src/ml"
  ml_files = sort([for f in fileset(local.ml_dir, "**") : f if !startswith(f, "tools/") && !strcontains(f, "__pycache__")])
  ml_tag   = substr(sha1(join("", [for f in local.ml_files : filesha1("${local.ml_dir}/${f}")])), 0, 12)
  ml_image = "${aws_ecr_repository.ml.repository_url}:${local.ml_tag}"
}

resource "aws_ecr_repository" "ml" {
  name                 = "kezekshi-ml"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # образы пересобираются из репо, хранить их не нужно

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "ml" {
  repository = aws_ecr_repository.ml.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# Сборка и загрузка образа. Тег = хэш файлов, поэтому новый образ появляется только при изменениях.
resource "terraform_data" "ml_image" {
  triggers_replace = [local.ml_image]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -e
      aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${split("/", aws_ecr_repository.ml.repository_url)[0]}
      if aws ecr describe-images --region ${var.aws_region} --repository-name ${aws_ecr_repository.ml.name} --image-ids imageTag=${local.ml_tag} >/dev/null 2>&1; then
        echo "kezekshi-ml: image ${local.ml_tag} already in ECR"
      else
        # Lambda принимает только обычный манифест образа: без provenance (buildx) или старым сборщиком
        if docker buildx version >/dev/null 2>&1; then
          docker build --platform linux/amd64 --provenance=false -t ${local.ml_image} ${local.ml_dir}
        else
          DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t ${local.ml_image} ${local.ml_dir}
        fi
        docker push ${local.ml_image}
        docker image rm ${local.ml_image} >/dev/null 2>&1 || true
      fi
    EOT
  }
}

resource "aws_iam_role" "ml" {
  name               = "kezekshi-ml-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "ml" {
  name              = "/aws/lambda/kezekshi-ml"
  retention_in_days = 14
}

data "aws_iam_policy_document" "ml" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ml.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ml" {
  name   = "ml"
  role   = aws_iam_role.ml.id
  policy = data.aws_iam_policy_document.ml.json
}

resource "aws_lambda_function" "ml" {
  function_name = "kezekshi-ml"
  role          = aws_iam_role.ml.arn
  package_type  = "Image"
  image_uri     = local.ml_image
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 30

  depends_on = [terraform_data.ml_image, aws_iam_role_policy.ml, aws_cloudwatch_log_group.ml]
}
