# Lambda kezekshi-llm: своя языковая модель (llama.cpp + открытая Qwen2.5-1.5B-Instruct, Apache 2.0).
# Пишет «Вероятную причину» инцидента живым языком; без неё анализатор шлёт шаблонный текст.
# Код: src/llm. Образ собирается при apply, когда меняется src/llm (нужен Docker, есть в CloudShell).
# Файл модели (~1,1 ГБ) один раз копируется с Hugging Face в закрытый бакет датасета, оттуда его
# берёт Lambda. Дообученную модель кладём под другим ключом и меняем var.llm_model_key.

variable "llm_model_url" {
  description = "Where to download the GGUF model once (Hugging Face)"
  type        = string
  default     = "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"
}

variable "llm_model_key" {
  description = "S3 key of the GGUF model in the dataset bucket"
  type        = string
  default     = "models/llm/qwen2.5-1.5b-instruct-q4_k_m.gguf"
}

variable "llm_memory_mb" {
  description = "Memory of kezekshi-llm; more memory = more CPU = faster text (new accounts may be capped at 3008)"
  type        = number
  default     = 3008
}

locals {
  llm_dir   = "${path.module}/../src/llm"
  llm_files = sort([for f in fileset(local.llm_dir, "**") : f if !strcontains(f, "__pycache__")])
  llm_tag   = substr(sha1(join("", [for f in local.llm_files : filesha1("${local.llm_dir}/${f}")])), 0, 12)
  llm_image = "${aws_ecr_repository.llm.repository_url}:${local.llm_tag}"
}

# Модель: Hugging Face -> S3 потоком, без копии на диске CloudShell
resource "terraform_data" "llm_model" {
  triggers_replace = [var.llm_model_url, var.llm_model_key, aws_s3_bucket.dataset.bucket]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -eo pipefail
      if aws s3api head-object --bucket ${aws_s3_bucket.dataset.bucket} --key ${var.llm_model_key} >/dev/null 2>&1; then
        echo "kezekshi-llm: model already in S3"
      else
        echo "kezekshi-llm: copying the model to S3 (~1.1 GB)"
        curl -fsSL "${var.llm_model_url}" | aws s3 cp - s3://${aws_s3_bucket.dataset.bucket}/${var.llm_model_key} --expected-size 2000000000 --only-show-errors
        echo "kezekshi-llm: model uploaded"
      fi
    EOT
  }
}

resource "aws_ecr_repository" "llm" {
  name                 = "kezekshi-llm"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
}

resource "aws_ecr_lifecycle_policy" "llm" {
  repository = aws_ecr_repository.llm.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 3 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 3 }
      action       = { type = "expire" }
    }]
  })
}

resource "terraform_data" "llm_image" {
  triggers_replace = [local.llm_image]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -e
      aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${split("/", aws_ecr_repository.llm.repository_url)[0]}
      if aws ecr describe-images --region ${var.aws_region} --repository-name ${aws_ecr_repository.llm.name} --image-ids imageTag=${local.llm_tag} >/dev/null 2>&1; then
        echo "kezekshi-llm: image ${local.llm_tag} already in ECR"
      else
        echo "kezekshi-llm: building the image (compiling llama.cpp takes 5-10 minutes)"
        if docker buildx version >/dev/null 2>&1; then
          docker build --platform linux/amd64 --provenance=false -t ${local.llm_image} ${local.llm_dir}
        else
          DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t ${local.llm_image} ${local.llm_dir}
        fi
        docker push ${local.llm_image}
        docker image rm ${local.llm_image} >/dev/null 2>&1 || true
        docker builder prune -af >/dev/null 2>&1 || true
        echo "kezekshi-llm: image pushed"
      fi
    EOT
  }
}

resource "aws_iam_role" "llm" {
  name               = "kezekshi-llm-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "llm" {
  name              = "/aws/lambda/kezekshi-llm"
  retention_in_days = 14
}

data "aws_iam_policy_document" "llm" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.llm.arn}:*"]
  }
  statement {
    sid       = "ReadModel"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.dataset.arn}/models/llm/*"]
  }
}

resource "aws_iam_role_policy" "llm" {
  name   = "llm"
  role   = aws_iam_role.llm.id
  policy = data.aws_iam_policy_document.llm.json
}

resource "aws_lambda_function" "llm" {
  function_name = "kezekshi-llm"
  role          = aws_iam_role.llm.arn
  package_type  = "Image"
  image_uri     = local.llm_image
  architectures = ["x86_64"]
  memory_size   = var.llm_memory_mb
  timeout       = 120

  ephemeral_storage {
    size = 3072 # модель (~1,1 ГБ) копируется в /tmp
  }

  environment {
    variables = {
      MODEL_BUCKET = aws_s3_bucket.dataset.bucket
      MODEL_KEY    = var.llm_model_key
      MODEL_NAME   = "qwen2.5-1.5b-instruct-q4"
    }
  }

  depends_on = [terraform_data.llm_image, terraform_data.llm_model, aws_iam_role_policy.llm, aws_cloudwatch_log_group.llm]
}
