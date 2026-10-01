# Chatbot Interface Infrastructure

Terraform for deploying the DQ chatbot Gradio UI (`collibra_dq_app/app.py`) on ECS Fargate.
No MCP server is deployed here -- the app talks directly to the JNJ Atlassian MCP server
(`MCP_ATLASSIAN_URL`), the JNJ GenAI gateway, Collibra DQ, Redshift and Postgres.

## Architecture

- **VPC**: existing VPC/subnets passed in as variables (not created here)
- **ALB**: public, HTTPS (ACM cert) with HTTP->HTTPS redirect; the HTTPS listener runs
  `authenticate-cognito` before forwarding to the Gradio target group (sticky sessions)
- **Cognito**: user pool federated to Entra ID via SAML; app client used by the ALB
- **ECS**: Fargate cluster/service running the Gradio UI container (port 7860) in private subnets
- **ECR**: repository for the Gradio UI image
- **SSM**: app secrets as SecureString parameters under `/<project>-<env>/app/*` (created manually)
- **IAM**: ECS execution role (ECR pull, logs, read app secrets) and task role (optional read-only S3)

## File Structure

- `backend.tf`: S3 backend and provider
- `main.tf`: locals and data sources
- `variables.tf` / `outputs.tf`
- `networking.tf`: existing VPC lookup and security groups
- `alb.tf`: ALB, target group, HTTP/HTTPS listeners with Cognito auth
- `cognito.tf`: user pool, Entra SAML IdP, ALB app client, hosted domain
- `ecs_gradio.tf`: ECS cluster, task definition (env + secrets), service
- `ecr.tf`, `iam.tf`, `ssm.tf`

## Usage

### Prerequisites

- Terraform 1.5.6, AWS CLI, Docker with buildx
- State bucket/lock table from `infra/1-terraform-init`
- Existing VPC with >= 2 public subnets (for the ALB) and private subnets with outbound
  access (NAT/TGW) to ECR, CloudWatch, SSM, and the JNJ endpoints the app calls
- ACM certificate in the deployment region covering `app_domain_name`
- Entra ID enterprise application (SAML) -- see [Entra ID setup](#entra-id-setup)

### 1. Create app secrets in SSM

One SecureString per name in `app_secret_names` (defaults in `variables.tf`). ECS fails to
start the task if any listed parameter is missing -- trim the list via tfvars if not needed.

```bash
export PREFIX=/dq-chatbot-dev/app
aws ssm put-parameter --type SecureString --name "$PREFIX/JNJ_GENAI_API_KEY" --value '...'
aws ssm put-parameter --type SecureString --name "$PREFIX/SUPERUSER_CREDENTIALS" --value 'alice:...'
# ...repeat for CDQ_*, REDSHIFT_*, DB_USER, DB_PASSWORD, X_ATLASSIAN_JIRA_PERSONAL_TOKEN
```

### 2. tfvars

```hcl
# dev.tfvars
environment         = "dev"
vpc_id              = "vpc-xxxx"
public_subnet_ids   = ["subnet-a", "subnet-b"]
private_subnet_ids  = ["subnet-c", "subnet-d"]
acm_certificate_arn = "arn:aws:acm:ap-southeast-1:123456789012:certificate/..."
app_domain_name     = "dq-chatbot-dev.example.jnj.com"
entra_metadata_url  = "https://login.microsoftonline.com/<tenant>/federationmetadata/2007-06/federationmetadata.xml?appid=<app-id>"

app_environment = {
  DB_HOST              = "..."
  DB_NAME              = "..."
  X_ATLASSIAN_USERNAME = "..."
}
# s3_read_bucket_names = ["itx-adj-anz-refined"]
```

Non-secret defaults (CDQ base URLs, `MCP_ATLASSIAN_URL`, etc.) are in `local.app_environment`
in `ecs_gradio.tf`; anything in `app_environment` overrides them.

### 3. Deploy

```bash
cd infra/2-chatbot-interface
export PROJECT_CODE=JIHS
export PROJECT_NAME=dq-chatbot
export ENVIRONMENT=dev
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
terraform init \
  -backend-config="bucket=${PROJECT_NAME}-${ENVIRONMENT}-${AWS_ACCOUNT_ID}-terraform-state" \
  -backend-config="key=${PROJECT_CODE}/${PROJECT_NAME}/${ENVIRONMENT}.tfstate" \
  -backend-config="dynamodb_table=${PROJECT_NAME}-terraform-lock-${ENVIRONMENT}"

terraform plan -out=plan.tfplan -var-file=${ENVIRONMENT}.tfvars
```

```bash
terraform apply plan.tfplan
```

### 4. DNS, image and service

1. Create a CNAME `app_domain_name` -> `alb_dns_name` output.
2. Build/push the image and roll the service (the service will fail health checks until
   the first image exists):

```bash
./scripts/build_gradio_ui.sh dev ap-southeast-1
```

## Entra ID setup

In the Entra enterprise application (SAML single sign-on), use the Terraform outputs:

- **Identifier (Entity ID)**: `entra_saml_entity_id`
- **Reply URL (ACS)**: `entra_saml_reply_url`
- Add a **groups** claim (mapped to `custom:<custom_attribute_name>` in Cognito)
- Set **Assignment required = Yes** and assign only the allowed users/groups -- the ALB
  authenticates but does not authorize by group, so access control happens here.

The ALB forwards the signed identity in the `x-amzn-oidc-data` header. Until the app reads
that header, users will still see the app's `SUPERUSER_CREDENTIALS` login after SSO.

### Environments

Resource names are prefixed with `<project_name>-<environment>`, so dev/test/prod can share
an account. Use one tfvars file and one state key per environment.

## Outputs

- `app_url`, `alb_dns_name`
- `cognito_user_pool_id`, `cognito_client_id`, `cognito_hosted_ui_url`
- `entra_saml_entity_id`, `entra_saml_reply_url`
- `ecr_repository_url`, `ecs_cluster_name`, `ecs_service_name`
- `app_secret_parameter_prefix`

### Things to do before deploying

-Create the SSM SecureString parameters listed in `app_secret_names`. The task won't start if one is missing; drop any you don't need (e.g. the CN credentials) in tfvars.
- Provide an ACM certificate and a domain name, then point a CNAME for that domain at the load balancer. Load-balancer authentication only works over HTTPS, so these are now required rather than optional.
- In the Entra app, enter the Entity ID and Reply URL from the outputs, and set **Assignment required = Yes**. The load balancer only checks who the user is, not which group they're in, so access control has to happen in Entra.
- After SSO, users will still see the app's `SUPERUSER_CREDENTIALS` login until the app reads the user identity the load balancer passes in the `x-amzn-oidc-data header`. That change to the app is still to do.