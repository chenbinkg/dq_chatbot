variable "project_name" {
  type = string
  default = "dq-chatbot"
}

variable "project_id" {
  type = string
  default = "JIHS"
}

variable "environment" {
  type = string
  default = "dev"
}

variable "service_owner" {
  type = string
  default = "Bryce Chen"
}

variable "service_category" {
  type = string
  default = "llm"
}

variable "authors" {
  type = string
  default = "Bryce Chen"
}

variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

# --- Networking (existing VPC) ---
variable "vpc_id" {
  type        = string
  description = "ID of the existing VPC to deploy into"
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Existing public subnet IDs (>= 2 AZs) for the ALB"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Existing private subnet IDs for ECS tasks; must have outbound access (NAT/TGW)"
}

variable "alb_ingress_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to reach the ALB on 80/443"
  default     = ["0.0.0.0/0"]
}

# --- TLS / domain (required for ALB Cognito authentication) ---
variable "acm_certificate_arn" {
  type        = string
  description = "ACM certificate ARN (same region) covering app_domain_name"
}

variable "app_domain_name" {
  type        = string
  description = "Public FQDN users browse to (CNAME to the ALB), e.g. dq-chatbot.example.jnj.com"
}

# --- ECS ---
variable "task_cpu" {
  type    = string
  default = "1024" # 1 vCPU
}

variable "task_memory" {
  type    = string
  default = "2048" # 2 GB
}

variable "service_desired_count" {
  type    = number
  default = 1
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "app_environment" {
  type        = map(string)
  description = "Extra/override non-secret env vars for the app (e.g. DB_HOST, DB_NAME, X_ATLASSIAN_USERNAME)"
  default     = {}
}

variable "app_secret_names" {
  type        = list(string)
  description = "Env var names injected from SSM SecureString parameters at /<project>-<env>/app/<NAME>"
  default = [
    "SUPERUSER_CREDENTIALS",
    "JNJ_GENAI_API_KEY",
    "CDQ_USERNAME_APAC",
    "CDQ_PASSWORD_APAC",
    "CDQ_USERNAME_CN",
    "CDQ_PASSWORD_CN",
    "REDSHIFT_USER",
    "REDSHIFT_PASSWORD",
    "DB_USER",
    "DB_PASSWORD",
    "X_ATLASSIAN_JIRA_PERSONAL_TOKEN",
  ]
}

variable "s3_read_bucket_names" {
  type        = list(string)
  description = "S3 buckets the agent's S3 tools may read (read-only). Empty = no S3 access."
  default     = []
}

# --- Cognito / Entra ID SAML ---
variable "entra_metadata_url" {
  type        = string
  description = "Entra ID SAML federation metadata URL for the enterprise application"
  sensitive   = true
}

variable "custom_attribute_name" {
  type        = string
  description = "Custom attribute name for group mapping"
  default     = "dq-chatbot"
}

variable "identity_provider_name" {
  type        = string
  description = "Identity provider name"
  default     = "EntraIDP"
}