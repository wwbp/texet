# -----------------------------------------------------------------------------
# logs.tf — CloudWatch log groups for the two services.
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name_prefix}/api"
  retention_in_days = 7
  tags              = { Name = "${local.name_prefix}-api-logs" }
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name_prefix}/worker"
  retention_in_days = 7
  tags              = { Name = "${local.name_prefix}-worker-logs" }
}
