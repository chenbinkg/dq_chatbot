terraform {
  required_version = "= 1.5.6"
  backend "s3" {
    encrypt        = true
    region         = "ap-southeast-1"
    # bucket, key and dynamodb_table are provided via -backend-config parameters
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.5.0"
    }
  }
}
provider "aws" {
  region = var.aws_region
}