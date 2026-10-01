#!/bin/bash

# Build the DQ chatbot Gradio image, push to ECR and roll the ECS service.
# Usage: ./build_gradio_ui.sh <environment> <aws-region> [image-tag]

set -euo pipefail

ENVIRONMENT=${1:-dev}
AWS_REGION=${2:-ap-southeast-1}
IMAGE_TAG=${3:-latest}
PROJECT_NAME=${PROJECT_NAME:-dq-chatbot}
NAME_PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$REPO_ROOT/infra/2-chatbot-interface"

echo "Building and deploying ${NAME_PREFIX} gradio UI in region: $AWS_REGION"

GRADIO_REPO=$(terraform -chdir="$TF_DIR" output -raw ecr_repository_url 2>/dev/null || echo "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${NAME_PREFIX}-gradio-ui")
CLUSTER_NAME=$(terraform -chdir="$TF_DIR" output -raw ecs_cluster_name 2>/dev/null || echo "${NAME_PREFIX}-cluster")
SERVICE_NAME=$(terraform -chdir="$TF_DIR" output -raw ecs_service_name 2>/dev/null || echo "${NAME_PREFIX}-gradio-ui")

echo "Repository: $GRADIO_REPO"

echo "Logging in to ECR..."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

# Repo root is the build context (app imports root-level modules); see Dockerfile.
echo "Building and pushing image with tag: $IMAGE_TAG..."
docker buildx build --platform linux/amd64 -f "$REPO_ROOT/Dockerfile" -t "$GRADIO_REPO:$IMAGE_TAG" --push "$REPO_ROOT"

echo "Updating ECS service $SERVICE_NAME on cluster $CLUSTER_NAME..."
aws ecs update-service \
    --cluster "$CLUSTER_NAME" \
    --service "$SERVICE_NAME" \
    --force-new-deployment \
    --region "$AWS_REGION" \
    --no-cli-pager \
    --output text > /dev/null

echo "Deployment complete!"
echo "Check service status with:"
echo "aws ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME --region $AWS_REGION"