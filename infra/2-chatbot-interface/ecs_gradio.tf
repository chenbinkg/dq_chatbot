# ECS Cluster for Gradio UI
resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"
  
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  
  tags = local.tags
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "gradio_ui" {
  name              = "/ecs/${local.name_prefix}-gradio-ui"
  retention_in_days = 30

  tags = local.tags
}

locals {
  app_environment = merge(
    {
      CDQ_BASE_URL_APAC          = "https://jnj-apac-comm-dq.collibra.jnj.com"
      CDQ_BASE_URL_CN            = "https://jnj-cn-comm-dq.myxjp.com"
      REDSHIFT_DBNAME            = "idiscover"
      DB_PORT                    = "5432"
      DB_SSLMODE                 = "require"
      MCP_ATLASSIAN_URL          = "https://atlassian-mcp.xena.dev/mcp/"
      X_ATLASSIAN_JIRA_URL       = "https://jira.jnj.com"
      X_ATLASSIAN_READ_ONLY_MODE = "true"
      X_ATLASSIAN_ENABLE_XRAY    = "false"
    },
    var.app_environment,
    {
      AWS_REGION         = var.aws_region
      PYTHONUNBUFFERED   = "1"
      GRADIO_SERVER_NAME = "0.0.0.0"
    }
  )

  # SecureString parameters must be created out-of-band (see README) so secrets never land in TF state.
  app_secret_param_prefix = "/${local.name_prefix}/app"
  app_secret_param_arn    = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.app_secret_param_prefix}"
}

# ECS Task Definition
resource "aws_ecs_task_definition" "gradio_ui" {
  family                   = "${local.name_prefix}-gradio-ui"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "gradio-ui-${var.environment}"
      image     = "${aws_ecr_repository.gradio_ui.repository_url}:${var.image_tag}"
      essential = true
      portMappings = [
        {
          containerPort = 7860
          hostPort      = 7860
          protocol      = "tcp"
        }
      ]
      environment = [for k, v in local.app_environment : { name = k, value = v }]
      secrets = [
        for name in var.app_secret_names : {
          name      = name
          valueFrom = "${local.app_secret_param_arn}/${name}"
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.gradio_ui.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "gradio-ui"
        }
      }
    }
  ])

  tags = local.tags
}

# ECS Service
resource "aws_ecs_service" "gradio_ui" {
  name            = "${local.name_prefix}-gradio-ui"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.gradio_ui.arn
  desired_count   = var.service_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  health_check_grace_period_seconds = 120

  load_balancer {
    target_group_arn = aws_lb_target_group.gradio_ui.arn
    container_name   = "gradio-ui-${var.environment}"
    container_port   = 7860
  }

  depends_on = [aws_lb_listener.https]

  tags = local.tags
}