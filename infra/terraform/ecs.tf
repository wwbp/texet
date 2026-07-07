# -----------------------------------------------------------------------------
# ecs.tf — Fargate cluster with two services sharing one image and one RDS:
#   api    — uvicorn behind the ALB, desired_count 1 (runs migrations on boot).
#   worker — `python -m app.worker`, desired_count var.worker_count (scaling lever).
# -----------------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name_prefix
  tags = { Name = "${local.name_prefix}-cluster" }
}

locals {
  image = "${aws_ecr_repository.texet.repository_url}:${var.image_tag}"

  # api runs FastAPI (mock mode) and owns migrations via the Dockerfile entrypoint.
  api_env = merge(local.common_env, {
    MAX_QUEUE_DEPTH   = tostring(var.max_queue_depth)
    SCHEDULER_ENABLED = "true"
  })

  # worker skips migrations (entrypoint guard) and runs the reply loop.
  worker_env = merge(local.common_env, {
    SKIP_MIGRATIONS    = "true"
    WORKER_CONCURRENCY = tostring(var.worker_concurrency)
  })
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name         = "api"
      image        = local.image
      essential    = true
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      environment  = [for k, v in local.api_env : { name = k, value = v }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }
    }
  ])

  tags = { Name = "${local.name_prefix}-api" }
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name        = "worker"
      image       = local.image
      essential   = true
      command     = ["uv", "run", "python", "-m", "app.worker"]
      environment = [for k, v in local.worker_env : { name = k, value = v }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])

  tags = { Name = "${local.name_prefix}-worker" }
}

resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command             = true
  health_check_grace_period_seconds  = 120 # allow alembic upgrade on boot
  deployment_minimum_healthy_percent = 0   # single task: allow replace on redeploy
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]

  tags = { Name = "${local.name_prefix}-api" }
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name_prefix}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_count
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true
  }

  tags = { Name = "${local.name_prefix}-worker" }
}
