# texet perf environment (Terraform, ECS Fargate, mock mode)

An **isolated, disposable** AWS environment for load-testing the reply-queue
architecture (`app/worker.py` + `app/queue.py`) with all external APIs mocked
(`MOCK_EXTERNAL_APIS=true`). It has its own VPC and RDS and shares nothing with
texet production (`twilio-texet/bot-prod`).

```
Internet ── ALB :80 ── api service (uvicorn, 1 task, runs migrations)
                          │
                          └── RDS Postgres (private) ──┐
                                                       │
                       worker service ─────────────────┘
                       (python -m app.worker, N tasks — the scaling lever)
```

Reply generation runs only in the worker service, so **scale reply throughput
with `worker_count` / `worker_concurrency`**, independent of the API. This is
what lets the AWS run reproduce local run 5b (workers absorb load; Postgres CPU
becomes the ceiling — see `docs/load-testing.md`).

## Prerequisites

- Terraform ≥ 1.9, Docker, AWS CLI v2, and the Session Manager plugin (for ECS Exec).
- AWS credentials for the target account (`aws sts get-caller-identity`).

## Runbook

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # optional edits

# 1. Create the infra (VPC, RDS, ECR, ECS, ALB). Services stay unhealthy until
#    the image is pushed in step 2 — expected.
terraform init
terraform apply   # scale via: -var api_desired_count=3 -var worker_count=3 -var worker_concurrency=80

# 2. Build + push the texet image (linux/amd64) to the new ECR repo, then let
#    ECS pull it.
./push-image.sh perf
CLUSTER=$(terraform output -raw cluster_name)
aws ecs update-service --cluster "$CLUSTER" --service "$(terraform output -raw api_service_name)"    --force-new-deployment
aws ecs update-service --cluster "$CLUSTER" --service "$(terraform output -raw worker_service_name)" --force-new-deployment

# NOTE on migrations: api tasks set SKIP_MIGRATIONS=true (so >1 api task doesn't
# race). On a FRESH database, run migrations once as a one-off ECS task before
# serving traffic (reuse the run-task pattern below with
# command ["uv","run","alembic","upgrade","head"]). The load-test runs here
# reused an already-migrated DB.

# 3. Wait for the api to go healthy, then create an API key via ECS Exec.
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$(terraform output -raw api_service_name)" --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container api --interactive \
  --command "uv run python -m app.auth.cli --name perf"
# copy the printed texet_... key

# 4. Run the load test from your laptop against the ALB.
cd ../..                                        # repo root
TEXET_API_KEY=<key> make load-perf HOST=$(terraform -chdir=infra/terraform output -raw alb_dns_name)

# 5. Scale workers and re-run to find the ceiling.
#    edit worker_count in terraform.tfvars, then:
terraform -chdir=infra/terraform apply

# 6. Tear everything down.
terraform -chdir=infra/terraform destroy
```

## Observing the run

- **Worker / api logs:** CloudWatch groups `/ecs/texet-perf/worker` and `/ecs/texet-perf/api`.
- **DB CPU (the expected ceiling):** RDS CloudWatch `CPUUtilization` for `texet-perf-db`.
- **Queue depth / stranding:** ECS Exec into the api task and query, e.g.
  `SELECT status, count(*) FROM utterances GROUP BY 1;`
- **Backpressure:** set `max_queue_depth` low (e.g. 200) and watch `/response`
  return 503 once the backlog fills.

## Notes & guardrails

- **Never point this at prod.** Everything is named `texet-perf-*` in a separate
  VPC/RDS. Mock mode means zero real OpenAI/Bedrock/SMS traffic and no charges
  for external calls.
- The ALB is internet-facing so you can drive load from a laptop. Set
  `allowed_ingress_cidr = "<your-ip>/32"` in tfvars to lock it down, and
  `terraform destroy` promptly when finished.
- **State is local** (`terraform.tfstate`, gitignored) — fine for a disposable
  env. To share it, add an `s3` backend block to `main.tf` and
  `terraform init -migrate-state`.
- RDS has `skip_final_snapshot=true` and ECR `force_delete=true`, so `destroy`
  is clean and leaves nothing behind.
