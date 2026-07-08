# -----------------------------------------------------------------------------
# rds.tf — Postgres for the perf environment. Private, disposable.
# -----------------------------------------------------------------------------

resource "random_password" "db" {
  length = 24
  # Keep to characters that are safe inside a URL without percent-encoding.
  special          = true
  override_special = "-_"
}

# Resolve the latest available Postgres 16 minor so we don't hardcode a version
# that ages out (and to avoid state drift from a major-only "16").
data "aws_rds_engine_version" "postgres" {
  engine  = "postgres"
  version = "16"
  latest  = true
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name_prefix}-db"
  engine         = "postgres"
  engine_version = data.aws_rds_engine_version.postgres.version
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = local.db_name
  username = local.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.database.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  # Disposable perf env: no backups, no final snapshot, destroyable.
  backup_retention_period = 0
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true

  tags = { Name = "${local.name_prefix}-db" }
}
