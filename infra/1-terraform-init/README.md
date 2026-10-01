# 1-terraform-init

The files in this directory set up Terraform [remote state](https://developer.hashicorp.com/terraform/language/state/remote). This is a prerequisite, to be completed before running other Terraform code. Remote state enables team work and is also a prerequisite to running Terraform in a CICD pipeline.

## How to run this?

1. Make sure you are authenticated with the right AWS account. You might want to check it with `aws sts get-caller-identity`
2. Run the following:
```
cd infra/1-terraform-init
export ENVIRONMENT=dev
export TF_VAR_environment=${ENVIRONMENT}

terraform init
terraform plan -out=plan.tfplan
terraform apply plan.tfplan
```

You may want to run the commands directly from your laptop, or use a [Dojo](https://github.com/kudulab/dojo)-docker container.

## Cleanup

Similar to above, the Terraform commands are:
```
cd infra/1-terraform-init
export ENVIRONMENT=dev
export TF_VAR_environment=${ENVIRONMENT}

terraform init
terraform plan -destroy -out=plan.tfplan
terraform apply plan.tfplan
```
