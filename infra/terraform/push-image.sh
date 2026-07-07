#!/usr/bin/env bash
# Build the texet image and push it to the perf ECR repo.
#
# Run AFTER `terraform apply` has created the ECR repo. Reads the repo URL and
# region from terraform outputs. Builds for linux/amd64 (Fargate's platform)
# so it works from Apple Silicon too.
#
# Usage:  ./push-image.sh [image_tag]      (default tag: perf)
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

image_tag="${1:-perf}"
ecr_url="$(terraform -chdir="${script_dir}" output -raw ecr_repository_url)"
region="${AWS_REGION:-$(aws configure get region || echo us-east-1)}"
registry="${ecr_url%%/*}"

echo "Building ${ecr_url}:${image_tag} (linux/amd64) from ${repo_root}/Dockerfile ..."
docker build --platform linux/amd64 -t "${ecr_url}:${image_tag}" "${repo_root}"

echo "Logging in to ${registry} ..."
aws ecr get-login-password --region "${region}" | docker login --username AWS --password-stdin "${registry}"

echo "Pushing ${ecr_url}:${image_tag} ..."
docker push "${ecr_url}:${image_tag}"

echo "Done. Force a fresh pull if the tag was reused:"
echo "  aws ecs update-service --cluster \$(terraform -chdir='${script_dir}' output -raw cluster_name) --service \$(terraform -chdir='${script_dir}' output -raw api_service_name) --force-new-deployment"
echo "  aws ecs update-service --cluster \$(terraform -chdir='${script_dir}' output -raw cluster_name) --service \$(terraform -chdir='${script_dir}' output -raw worker_service_name) --force-new-deployment"
