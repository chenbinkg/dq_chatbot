# Application Load Balancer
resource "aws_lb" "main" {
  name               = "${local.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
  # Long-running agent turns stream over SSE; the 60s default can cut them off.
  idle_timeout               = 300
  drop_invalid_header_fields = true

  enable_deletion_protection = var.environment == "prod"

  tags = local.tags
}

resource "aws_lb_target_group" "gradio_ui" {
  name        = "${local.name_prefix}-tg"
  port        = 7860
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.main.id
  target_type = "ip"

  # Agent + interrupt state lives in-memory on each task, so pin users to one task.
  stickiness {
    type            = "lb_cookie"
    cookie_duration = 28800
    enabled         = true
  }

  health_check {
    enabled             = true
    interval            = 30
    path                = "/"
    port                = "traffic-port"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    timeout             = 5
    matcher             = "200-299"
  }

  tags = local.tags
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type  = "authenticate-cognito"
    order = 1

    authenticate_cognito {
      user_pool_arn              = aws_cognito_user_pool.main.arn
      user_pool_client_id        = aws_cognito_user_pool_client.client.id
      user_pool_domain           = aws_cognito_user_pool_domain.main.domain
      scope                      = "openid email"
      session_timeout            = 28800
      on_unauthenticated_request = "authenticate"
    }
  }

  default_action {
    type             = "forward"
    order            = 2
    target_group_arn = aws_lb_target_group.gradio_ui.arn
  }

  tags = local.tags
}
