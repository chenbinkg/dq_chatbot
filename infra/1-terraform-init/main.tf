locals {
  tags = {
    creation_method  = "terraform"
    Authors       = var.authors
    ServiceOwner = var.service_owner
    ServiceCategory = var.service_category
    Project = var.project_id
    ProjectName = var.project_name
    Environment = var.environment
  }
  name_prefix = "${var.project_name}-${var.environment}"
}

resource "aws_s3_bucket" "terraform_setup" {
  #checkov:skip=CKV_AWS_18: "Ensure the S3 bucket has access logging enabled" - no need here, use Amazon CloudTrail instead
  #checkov:skip=CKV_AWS_144: "Ensure that S3 bucket has cross-region replication enabled"
  #checkov:skip=CKV2_AWS_61: "Ensure that an S3 bucket has a lifecycle configuration"
  #checkov:skip=CKV2_AWS_62: "Ensure S3 buckets should have event notifications enabled"
  bucket        = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-terraform-state"
  tags          = local.tags
  force_destroy = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_setup" {
  bucket = aws_s3_bucket.terraform_setup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_versioning"  "terraform_setup" {
  bucket = aws_s3_bucket.terraform_setup.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_setup" {
  bucket                  = aws_s3_bucket.terraform_setup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "terraform_setup" {
  #checkov:skip=CKV2_AWS_16: "Ensure that Auto Scaling is enabled on your DynamoDB tables"
  #checkov:skip=CKV_AWS_28: "Ensure DynamoDB point in time recovery (backup) is enabled"
  #checkov:skip=CKV_AWS_119: "Ensure DynamoDB Tables are encrypted using a KMS Customer Managed CMK"
  name           = "${var.project_name}-terraform-lock-${var.environment}"
  read_capacity  = 5
  write_capacity = 5
  hash_key       = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
  tags = local.tags
}

resource "local_file" "terraform_states_s3_bucket_name" {
  content  = aws_s3_bucket.terraform_setup.id
  filename = "tf_output_terraform_states_s3_bucket_name.txt"
}
