# -----------------------------------------------------------------------------
# variables.tf — inputs for the texet perf environment. All have defaults so a
# bare `terraform apply` stands up a working stack; override in terraform.tfvars.
# -----------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name. Keeps this stack isolated from prod."
  type        = string
  default     = "texet-perf"
}

variable "image_tag" {
  description = "Tag of the texet image in ECR that the ECS tasks run (push-image.sh pushes this)."
  type        = string
  default     = "perf"
}

# --- ECS sizing -------------------------------------------------------------

variable "api_cpu" {
  description = "Fargate CPU units for the api task (accept-only; not the bottleneck)."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the api task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = "Number of api tasks behind the ALB. >1 requires migrations to run out-of-band (api tasks set SKIP_MIGRATIONS)."
  type        = number
  default     = 1
}

variable "worker_cpu" {
  description = "Fargate CPU units per worker task."
  type        = number
  default     = 1024
}

variable "worker_memory" {
  description = "Fargate memory (MiB) per worker task."
  type        = number
  default     = 2048
}

variable "worker_count" {
  description = "Number of worker tasks. This is the reply-throughput scaling lever."
  type        = number
  default     = 3
}

# --- App tuning (map to the queue-architecture env vars) --------------------

variable "worker_concurrency" {
  description = "WORKER_CONCURRENCY: concurrent claim/process loops per worker task."
  type        = number
  default     = 80
}

variable "max_queue_depth" {
  description = "MAX_QUEUE_DEPTH: 503 backpressure threshold on queued+processing replies (0 disables)."
  type        = number
  default     = 1000
}

variable "db_pool_size" {
  description = "DB_POOL_SIZE for both api and worker tasks."
  type        = number
  default     = 20
}

variable "db_max_overflow" {
  description = "DB_MAX_OVERFLOW for both api and worker tasks."
  type        = number
  default     = 40
}

variable "mock_llm_latency_ms" {
  description = "Simulated LLM latency (ms) when MOCK_EXTERNAL_APIS is on."
  type        = number
  default     = 1500
}

variable "mock_moderation_latency_ms" {
  description = "Simulated moderation latency (ms)."
  type        = number
  default     = 300
}

variable "mock_sms_latency_ms" {
  description = "Simulated outbound SMS latency (ms)."
  type        = number
  default     = 150
}

# --- Database ---------------------------------------------------------------

variable "db_instance_class" {
  description = "RDS instance class. DB CPU was the run-5b ceiling, so default gives headroom."
  type        = string
  default     = "db.t3.medium"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage (GiB)."
  type        = number
  default     = 20
}

# --- Ingress guardrail ------------------------------------------------------

variable "allowed_ingress_cidr" {
  description = "CIDR allowed to reach the ALB on :80. Default is open (needed for laptop load-gen); set to <your-ip>/32 to lock down."
  type        = string
  default     = "0.0.0.0/0"
}
