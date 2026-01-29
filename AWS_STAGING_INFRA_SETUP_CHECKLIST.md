# AWS staging infra setup checklist (template)

Use this for a first-time staging setup. It is a checklist, not a run log.
For the actual run, use a private/internal document.

## Goal

- Staging API on Elastic Beanstalk (single-container Docker)
- RDS PostgreSQL in private subnets
- Security groups allow EB -> RDS
- Environment variables set on EB

## Before you start

- [ ] Pick AWS region
- [ ] Pick EB app + environment names
- [ ] Pick RDS instance identifier + DB name
- [ ] Decide VPC: use existing or create a new one
- [ ] Decide who owns admin console credentials

## VPC and networking

- [ ] VPC exists with public + private subnets
- [ ] Public subnets for the EB load balancer (ALB)
- [ ] Private subnets for EB instances and RDS
- [ ] Internet gateway + public route table
- [ ] NAT gateway + private route table

## RDS (PostgreSQL)

- [ ] Create RDS instance (private, not public)
- [ ] Create DB subnet group (private subnets)
- [ ] Create RDS security group
- [ ] Allow inbound 5432 from EB security group
- [ ] Record endpoint + port

## Elastic Beanstalk

- [ ] Create EB application
- [ ] Create EB environment (Docker single container, load balanced)
- [ ] Configure EB VPC: ALB in public subnets, instances in private subnets
- [ ] Set health check path to `/health`
- [ ] Confirm EB instance security group
- [ ] Review and update EB environment variables before deploys

## Required environment variables

- [ ] `DATABASE_URL`
- [ ] `OPENAI_API_KEY`
- [ ] `OPENAI_MODEL`
- [ ] `SMS_OUTBOUND_URL`

Optional:

- [ ] `SMS_OUTBOUND_AUTHORIZATION`
- [ ] `SMS_TIMEOUT_SECONDS`
- [ ] `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_SECRET_KEY`
- [ ] `ADMIN_SESSION_TTL_SECONDS`

## First deploy (manual)

- [ ] Deploy with EB Console or `eb deploy`
- [ ] Do NOT upload `docker-compose.yml` to EB (local only)
- [ ] Run migrations: `alembic upgrade head`
- [ ] Create API key: `python -m app.auth.cli`
- [ ] Verify:
  - [ ] `https://<eb-url>/health`
  - [ ] `https://<eb-url>/db/health`

## After first deploy

- [ ] Record EB URL, RDS endpoint, and region in your private run log
- [ ] Store API key in your secret manager
- [ ] Confirm admin console login (if enabled)

## CI/CD note

This checklist is only for first-time manual setup. Once CI/CD (GitHub Actions)
is in place, deployments should use CI/CD and not manual zips or local builds.
