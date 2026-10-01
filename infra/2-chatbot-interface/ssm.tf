# Non-secret infra config for reference by scripts/ops. App secrets live under
# /<name_prefix>/app/* and are created manually (see README).
resource "aws_ssm_parameter" "aws_region" {
  name  = "/${local.name_prefix}/aws-region"
  type  = "String"
  value = var.aws_region
  tags  = local.tags
}

resource "aws_ssm_parameter" "alb_dns_name" {
  name  = "/${local.name_prefix}/alb-dns-name"
  type  = "String"
  value = aws_lb.main.dns_name
  tags  = local.tags
}

resource "aws_ssm_parameter" "cognito_user_pool_id" {
  name  = "/${local.name_prefix}/cognito-user-pool-id"
  type  = "String"
  value = aws_cognito_user_pool.main.id
  tags  = local.tags
}

resource "aws_ssm_parameter" "cognito_client_id" {
  name  = "/${local.name_prefix}/cognito-client-id"
  type  = "String"
  value = aws_cognito_user_pool_client.client.id
  tags  = local.tags
}
