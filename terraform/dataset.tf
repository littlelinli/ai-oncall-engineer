# Датасет ShopSim для ML: закрытый бакет, демо-сервер раз в час выгружает туда CSV
# (SIM_S3_BUCKET в /etc/shopsim.env на сервере).

resource "aws_s3_bucket" "dataset" {
  bucket = "kezekshi-dataset-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "dataset" {
  bucket                  = aws_s3_bucket.dataset.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "dataset_upload" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.dataset.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.dataset.arn}/*"]
  }
}

resource "aws_iam_role_policy" "dataset_upload" {
  name   = "shopsim-dataset-upload"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.dataset_upload.json
}
