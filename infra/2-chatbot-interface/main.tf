# Data sources
data "aws_caller_identity" "current" {}

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
