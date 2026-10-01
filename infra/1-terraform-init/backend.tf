terraform {
  required_version = "= 1.5.6"
  backend "local" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 5.56.1"
    }
    local = {
      source  = "hashicorp/local"
      version = "= 2.1.0"
    }
  }
}
provider "aws" {
  region = "ap-southeast-1"
}
