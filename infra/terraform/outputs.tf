# -----------------------------------------------------------------------------
# outputs.tf — values needed to push the image, run the load test, and inspect.
# -----------------------------------------------------------------------------

output "alb_dns_name" {
  description = "Load-test target: TEXET_API_KEY=... make load-perf HOST=http://<this>"
  value       = "http://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  description = "Push the texet image here (see push-image.sh)."
  value       = aws_ecr_repository.texet.repository_url
}

output "cluster_name" {
  description = "ECS cluster (for aws ecs execute-command / describe)."
  value       = aws_ecs_cluster.main.name
}

output "api_service_name" {
  value = aws_ecs_service.api.name
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "rds_address" {
  description = "RDS endpoint (private; reachable only from the ECS task SG)."
  value       = aws_db_instance.main.address
  sensitive   = true
}

output "database_url" {
  description = "Assembled async DATABASE_URL injected into the tasks."
  value       = local.database_url
  sensitive   = true
}
