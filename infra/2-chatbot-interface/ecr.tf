# ECR Repository for Gradio UI
resource "aws_ecr_repository" "gradio_ui" {
  name                 = "${local.name_prefix}-gradio-ui"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.tags
}