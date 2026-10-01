# IAM Role for ECS Task Execution
resource "aws_iam_role" "ecs_task_execution" {
  name = "${local.name_prefix}-ecs-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      },
    ]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Lets ECS inject the app's SSM SecureString parameters as container secrets.
resource "aws_iam_role_policy" "ecs_execution_app_secrets" {
  name = "${local.name_prefix}-app-secrets"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["ssm:GetParameters"]
        Effect   = "Allow"
        Resource = "${local.app_secret_param_arn}/*"
      },
    ]
  })
}

# IAM Role for ECS Task
resource "aws_iam_role" "ecs_task" {
  name = "${local.name_prefix}-ecs-task-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      },
    ]
  })

  tags = local.tags
}

# Read-only S3 access for the list_s3_objects / sample_s3_file agent tools.
resource "aws_iam_role_policy" "ecs_task_s3_read" {
  count = length(var.s3_read_bucket_names) > 0 ? 1 : 0

  name = "${local.name_prefix}-s3-read"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["s3:ListBucket"]
        Effect   = "Allow"
        Resource = [for b in var.s3_read_bucket_names : "arn:aws:s3:::${b}"]
      },
      {
        Action   = ["s3:GetObject"]
        Effect   = "Allow"
        Resource = [for b in var.s3_read_bucket_names : "arn:aws:s3:::${b}/*"]
      },
    ]
  })
}