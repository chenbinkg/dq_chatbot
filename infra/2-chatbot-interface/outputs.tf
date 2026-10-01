output "alb_dns_name" {
  description = "ALB DNS name -- create a CNAME from app_domain_name to this"
  value       = aws_lb.main.dns_name
}

output "app_url" {
  description = "URL users browse to"
  value       = "https://${var.app_domain_name}/"
}

output "cognito_user_pool_id" {
  description = "The ID of the Cognito User Pool"
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "The ID of the Cognito User Pool Client"
  value       = aws_cognito_user_pool_client.client.id
}

output "ecr_repository_url" {
  description = "The URL of the Gradio UI ECR repository"
  value       = aws_ecr_repository.gradio_ui.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.gradio_ui.name
}

output "app_secret_parameter_prefix" {
  description = "SSM path where app SecureString secrets must be created"
  value       = local.app_secret_param_prefix
}

output "cognito_hosted_ui_url" {
  description = "Cognito Hosted UI base URL"
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
}

# Values to enter in the Entra ID enterprise app's SAML configuration.
output "entra_saml_entity_id" {
  value = "urn:amazon:cognito:sp:${aws_cognito_user_pool.main.id}"
}

output "entra_saml_reply_url" {
  value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com/saml2/idpresponse"
}
