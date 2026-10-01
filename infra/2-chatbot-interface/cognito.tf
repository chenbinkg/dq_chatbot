# Cognito User Pool for Authentication
resource "aws_cognito_user_pool" "main" {
  name = "${local.name_prefix}-user-pool"
  
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Users only arrive via Entra SSO; block self sign-up.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  
  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_numbers   = true
    require_symbols   = true
    require_uppercase = true
  }
  
  # Custom attribute for group mapping
  schema {
    name                = var.custom_attribute_name
    attribute_data_type = "String"
    mutable             = true
  }
  
  lifecycle {
    ignore_changes = [schema]
  }
  
  tags = local.tags
}

# Entra ID SAML Identity Provider
resource "aws_cognito_identity_provider" "entra" {
  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = var.identity_provider_name
  provider_type = "SAML"

  provider_details = {
    MetadataURL = var.entra_metadata_url
  }

  attribute_mapping = {
    email                      = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
    "custom:${var.custom_attribute_name}" = "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"
  }
}

# App client used by the ALB authenticate-cognito action (ALB requires a client secret).
resource "aws_cognito_user_pool_client" "client" {
  name                = "${local.name_prefix}-client"
  user_pool_id        = aws_cognito_user_pool.main.id
  generate_secret     = true
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  allowed_oauth_flows_user_pool_client = true
  callback_urls                        = ["https://${var.app_domain_name}/oauth2/idpresponse"]
  logout_urls                          = ["https://${var.app_domain_name}/"]
  supported_identity_providers         = [aws_cognito_identity_provider.entra.provider_name]
  prevent_user_existence_errors        = "ENABLED"

  depends_on = [aws_cognito_identity_provider.entra]
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name_prefix}-auth"
  user_pool_id = aws_cognito_user_pool.main.id
}

