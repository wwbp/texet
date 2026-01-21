# AWS Staging Setup (Elastic Beanstalk + RDS)

## Who this is for
This is a white-glove, step-by-step guide for setting up a staging version of the Texet API on AWS.
It is written for non-technical readers who can follow checklists and copy/paste commands.

## What you will end up with
- A staging API on AWS Elastic Beanstalk.
- A PostgreSQL database on Amazon RDS.
- A repeatable setup log so anyone can redo the same environment later.

## Keep a short log as you go
Write any decisions, errors, or changes in `WORKING_LOG.md` so the setup is reproducible.

## Quick glossary
- Elastic Beanstalk (EB): AWS service that runs your application.
- RDS: AWS service that hosts the database.
- Security group: a firewall rule list for AWS resources.
- VPC: the private network that holds EB and RDS.

## Design choices (staging)
- Platform: EB Docker single-container (Amazon Linux 2 solution stack).
- Environment type: single instance for cost.
- Database: RDS PostgreSQL, single-AZ, not publicly accessible.
- Networking: RDS allows inbound 5432 only from the EB instance security group.
- Secrets: set as EB environment properties (plan to move later).
- Migrations: run Alembic manually on each deploy for now.
- Timezone: app uses EST; RDS can remain at default UTC.
- Defaults we will use:
  - EB instance type: `t3.small`
  - RDS instance class: `db.t3.small`
  - RDS storage: 20 GB `gp3`
  - Backups: 7 days
  - Deletion protection: off (staging only)

## This run (fill these in as we go)
- AWS account ID: `336162656437`
- AWS region: `us-east-1`
- EB application name: `texet`
- EB environment name: `staging`
- EB platform: `64bit Amazon Linux 2 v4.5.1 running Docker`
- EB environment ID: `e-r3besxmnqq`
- EB CNAME: `staging.eba-bpsukg5a.us-east-1.elasticbeanstalk.com`
- ALB ARN: `arn:aws:elasticloadbalancing:us-east-1:336162656437:loadbalancer/app/awseb--AWSEB-enmMFtINCNH1/11564bb15f5cb9d0`
- ALB scheme: `internal`
- EB instance security group: `sg-04112491ee790f629`
- EB load balancer security group: `sg-0a33ba68188f79084`
- EB platform: `Python 3.12 running on 64bit Amazon Linux 2023`
- RDS instance identifier: `texet-staging`
- RDS engine: `postgres`
- RDS master username: `svadmin`
- RDS database name: `texet`
- RDS private: yes
- EB instance type: `t3.small`
- RDS instance class: `db.t3.small`
- RDS storage: 20 GB `gp3`
- Backups: 7 days
- Deletion protection: off
- VPC name: `texet-stage`
- VPC ID: `vpc-0d60f9f1c7466d50c`
- Public subnets: `subnet-001390894d606adac` (1a), `subnet-057d3f8d8e4c47a56` (1b)
- Private subnets: `subnet-0379b68d60f5a703c` (1a), `subnet-0d62724cfd2e24b62` (1b)
- Internet gateway: `igw-0e892072d020ab640`
- Public route table: `rtb-0323274540c8aed22`
- RDS security group: `sg-0965c3ed6df818390`
- RDS subnet group: `texet-stage-db-subnets`
- RDS endpoint: `texet-staging.csvus00s8xa2.us-east-1.rds.amazonaws.com`
- RDS port: `5432`
- NAT gateway: `nat-0b5e88222f57b7893`
- NAT EIP allocation: `eipalloc-0d1602f30d004c7ba`
- Private route table: `rtb-0d78cfd0243a795d1`
- EB key pair: `texet-stage-key`

## Info to collect before you start
Fill these in as you go:
- AWS account ID:
- AWS region (example: us-east-1):
- EB application name:
- EB environment name:
- RDS instance identifier:
- RDS database name:
- RDS master username:
- RDS endpoint and port:
- `DATABASE_URL` to use:
- Do we require SSL/TLS for DB? (yes/no)
- Admin UI enabled? (yes/no)

## Required configuration values
Required for chat:
- `API_TOKEN` (shared secret for the `/chat` API)
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `SMS_OUTBOUND_URL`
- `DATABASE_URL` (RDS connection string)

Optional:
- `SMS_TIMEOUT_SECONDS`
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_SECRET_KEY`
- `ADMIN_SESSION_TTL_SECONDS`
- `ADMIN_EXPORT_MAX_ROWS`

Example `DATABASE_URL`:
`postgresql+asyncpg://texet_user:<password>@<rds-endpoint>:5432/texet`

If RDS requires SSL/TLS, append `?sslmode=require`.

## Step-by-step setup

### Step 0: Create a VPC (only if you do not already have one)
We did not have a default VPC, so we created a new one named `texet-stage`.
If you already have a suitable VPC, skip this step.

What we created:
- One VPC: `10.50.0.0/16`
- Two public subnets (for EB) in 2 availability zones.
- Two private subnets (for RDS) in 2 availability zones.
- An internet gateway and a public route table.
- A NAT gateway so private instances can access the internet.

#### Command log (this run)
We ran these CLI commands and recorded the IDs they returned.
Keep this list for reproducibility.

Create the VPC:
```bash
aws ec2 create-vpc --cidr-block 10.50.0.0/16 --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=texet-stage}]" --region us-east-1 --query "Vpc.VpcId" --output text
```
Expected output: `vpc-0d60f9f1c7466d50c`

Enable DNS support and hostnames:
```bash
aws ec2 modify-vpc-attribute --vpc-id vpc-0d60f9f1c7466d50c --enable-dns-support "{\"Value\":true}" --region us-east-1
aws ec2 modify-vpc-attribute --vpc-id vpc-0d60f9f1c7466d50c --enable-dns-hostnames "{\"Value\":true}" --region us-east-1
```
Expected output: no output (success)

Create and attach internet gateway:
```bash
aws ec2 create-internet-gateway --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=texet-stage-igw}]" --region us-east-1 --query "InternetGateway.InternetGatewayId" --output text
aws ec2 attach-internet-gateway --internet-gateway-id igw-0e892072d020ab640 --vpc-id vpc-0d60f9f1c7466d50c --region us-east-1
```
Expected output: `igw-0e892072d020ab640` (first command), no output (second command)

Create public subnets:
```bash
aws ec2 create-subnet --vpc-id vpc-0d60f9f1c7466d50c --availability-zone us-east-1a --cidr-block 10.50.0.0/24 --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=texet-stage-public-1a}]" --region us-east-1 --query "Subnet.SubnetId" --output text
aws ec2 create-subnet --vpc-id vpc-0d60f9f1c7466d50c --availability-zone us-east-1b --cidr-block 10.50.1.0/24 --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=texet-stage-public-1b}]" --region us-east-1 --query "Subnet.SubnetId" --output text
```
Expected outputs: `subnet-001390894d606adac`, `subnet-057d3f8d8e4c47a56`

Create private subnets:
```bash
aws ec2 create-subnet --vpc-id vpc-0d60f9f1c7466d50c --availability-zone us-east-1a --cidr-block 10.50.10.0/24 --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=texet-stage-private-1a}]" --region us-east-1 --query "Subnet.SubnetId" --output text
aws ec2 create-subnet --vpc-id vpc-0d60f9f1c7466d50c --availability-zone us-east-1b --cidr-block 10.50.11.0/24 --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=texet-stage-private-1b}]" --region us-east-1 --query "Subnet.SubnetId" --output text
```
Expected outputs: `subnet-0379b68d60f5a703c`, `subnet-0d62724cfd2e24b62`

Enable public IPs on public subnets:
```bash
aws ec2 modify-subnet-attribute --subnet-id subnet-001390894d606adac --map-public-ip-on-launch --region us-east-1
aws ec2 modify-subnet-attribute --subnet-id subnet-057d3f8d8e4c47a56 --map-public-ip-on-launch --region us-east-1
```
Expected output: no output (success)

Create and wire the public route table:
```bash
aws ec2 create-route-table --vpc-id vpc-0d60f9f1c7466d50c --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=texet-stage-public-rt}]" --region us-east-1 --query "RouteTable.RouteTableId" --output text
aws ec2 create-route --route-table-id rtb-0323274540c8aed22 --destination-cidr-block 0.0.0.0/0 --gateway-id igw-0e892072d020ab640 --region us-east-1
aws ec2 associate-route-table --route-table-id rtb-0323274540c8aed22 --subnet-id subnet-001390894d606adac --region us-east-1
aws ec2 associate-route-table --route-table-id rtb-0323274540c8aed22 --subnet-id subnet-057d3f8d8e4c47a56 --region us-east-1
```
Expected outputs: `rtb-0323274540c8aed22` (first command), then JSON with `"Return": true`, then association IDs.

Create RDS security group and subnet group:
```bash
aws ec2 create-security-group --group-name texet-stage-rds-sg --description "RDS security group for texet staging" --vpc-id vpc-0d60f9f1c7466d50c --region us-east-1 --query "GroupId" --output text
aws rds create-db-subnet-group --db-subnet-group-name texet-stage-db-subnets --db-subnet-group-description "Private subnets for texet staging RDS" --subnet-ids subnet-0379b68d60f5a703c subnet-0d62724cfd2e24b62 --region us-east-1
```
Expected output: `sg-0965c3ed6df818390` and JSON confirming the subnet group.

Create a NAT gateway for private subnets:
```bash
aws ec2 allocate-address --domain vpc --region us-east-1 --query "AllocationId" --output text
aws ec2 create-nat-gateway --subnet-id subnet-001390894d606adac --allocation-id eipalloc-0d1602f30d004c7ba --tag-specifications "ResourceType=natgateway,Tags=[{Key=Name,Value=texet-stage-nat}]" --region us-east-1 --query "NatGateway.NatGatewayId" --output text
aws ec2 wait nat-gateway-available --nat-gateway-ids nat-0b5e88222f57b7893 --region us-east-1
```
Expected output: EIP allocation ID, NAT gateway ID, then no output from the wait.

Create a private route table and route outbound traffic through NAT:
```bash
aws ec2 create-route-table --vpc-id vpc-0d60f9f1c7466d50c --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=texet-stage-private-rt}]" --region us-east-1 --query "RouteTable.RouteTableId" --output text
aws ec2 create-route --route-table-id rtb-0d78cfd0243a795d1 --destination-cidr-block 0.0.0.0/0 --nat-gateway-id nat-0b5e88222f57b7893 --region us-east-1
aws ec2 associate-route-table --route-table-id rtb-0d78cfd0243a795d1 --subnet-id subnet-0379b68d60f5a703c --region us-east-1
aws ec2 associate-route-table --route-table-id rtb-0d78cfd0243a795d1 --subnet-id subnet-0d62724cfd2e24b62 --region us-east-1
```
Expected output: route table ID, JSON with `"Return": true`, then association IDs.

Create an EC2 key pair for EB SSH:
```bash
aws ec2 create-key-pair --key-name texet-stage-key --key-type rsa --key-format pem --region us-east-1 --query "KeyMaterial" --output text > texet-stage-key.pem
chmod 600 texet-stage-key.pem
```
Expected output: no output; the private key is saved to `texet-stage-key.pem`.

### Step 1: Create the database (RDS)
1. Open the AWS Console and search for "RDS".
2. Click "Create database".
3. Choose:
   - Engine: PostgreSQL.
   - Template: Dev/Test (good for staging).
   - Deployment: Single-AZ.
4. Set:
   - DB instance identifier (example: `texet-staging`).
   - Master username and password (store the password securely).
   - DB name (example: `texet`).
5. Connectivity:
   - Public access: No.
   - VPC: default is fine unless your organization uses a custom one.
   - Note: If you create the DB before the app, you will update its security group
     later to allow inbound traffic from the EB environment.

Note: RDS master usernames cannot include hyphens. Use letters, numbers, or underscore
(example: `sv_admin`).

If you choose AWS-managed passwords and see `KMSKeyNotAccessibleFault`, you may need
to pick a KMS key you can access or use a manually generated password instead.

#### Command log (this run)
We used a manually generated master password because AWS-managed passwords failed
with a KMS key access error.

Create the RDS instance:
```bash
aws rds create-db-instance --db-instance-identifier texet-staging --engine postgres --engine-version 16.9 --db-instance-class db.t3.small --allocated-storage 20 --storage-type gp3 --master-username svadmin --master-user-password '<REDACTED>' --db-name texet --db-subnet-group-name texet-stage-db-subnets --vpc-security-group-ids sg-0965c3ed6df818390 --backup-retention-period 7 --no-publicly-accessible --no-deletion-protection --region us-east-1
```
Expected output: JSON with `"DBInstanceIdentifier": "texet-staging"` and status `"creating"`.
6. Create the database and wait for status "Available".
7. Record the DB endpoint and port.

### Step 2: Create the app (Elastic Beanstalk)
1. Open the AWS Console and search for "Elastic Beanstalk".
2. Click "Create application".
3. Set:
   - Application name (example: `texet`).
   - Platform: Docker (Amazon Linux 2 solution stack).
   - Environment: Web server environment.
4. Choose:
   - Environment name (example: `texet-staging`).
   - Single instance (no load balancer for now).
5. Create the environment and wait for "Health: Green".

### Step 3: Lock down network access
1. Find the EB environment security group (in the EB console).
2. Edit the RDS security group inbound rules:
   - Allow TCP 5432 from the EB security group only.
3. Leave RDS public access disabled.

If the load balancer is internal, you can still test by SSH-ing into the
instance and curling `http://localhost:8000/health`.

### Step 4: Set app settings (environment variables)
1. In the EB console, open Configuration -> Software.
2. Add the required environment properties:
   - `API_TOKEN`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `SMS_OUTBOUND_URL`, `DATABASE_URL`.
3. Add optional admin settings only if you need the admin UI.

### Step 5: Deploy the app
Preferred (repeatable) CLI path:
1. Install AWS CLI and EB CLI.
2. Ensure the repo has a `Dockerfile` at the root.
3. From the repo root:
   - `eb init -p "64bit Amazon Linux 2 v4.5.1 running Docker" texet --region <region>`
   - `eb create staging --elb-type application --instance_type t3.small --keyname texet-stage-key --vpc.id vpc-0d60f9f1c7466d50c --vpc.ec2subnets subnet-0379b68d60f5a703c,subnet-0d62724cfd2e24b62 --vpc.elbsubnets subnet-001390894d606adac,subnet-057d3f8d8e4c47a56 --vpc.publicip false`
   - `eb deploy`

Console path (if you do not use CLI):
1. In EB, create an application version from a zip of the repo.
2. Deploy that version to the environment.

Troubleshooting:
- If `eb init` fails with `EndpointConnectionError` but AWS CLI works, use the AWS
  CLI or Console to create the EB application and environment.
- If you see `NotAuthorizedError` for `elasticbeanstalk:CreateApplication`, ask your
  AWS admin to grant EB permissions or use a profile with access.
- If `AWSElasticBeanstalkFullAccess` is not available, use
  `AdministratorAccess-AWSElasticBeanstalk` instead.
- If permissions are still denied after attaching the policy, check for org-level
  SCP restrictions or use an admin profile.
- EB CLI may also need `s3:CreateBucket` to create the EB artifacts bucket.
  If blocked, have an admin pre-create `elasticbeanstalk-<region>-<account>`
  (example: `elasticbeanstalk-us-east-1-336162656437`).
- EB also needs ELBv2 permissions (for ALB + target groups). If you see
  `elasticloadbalancing:DescribeLoadBalancers` or `DescribeTargetGroups` errors,
  grant ELBv2 permissions or attach a broader policy.
- EB also needs Auto Scaling permissions (for ASG creation). If you see
  `autoscaling:DescribeAutoScalingGroups` errors, grant Auto Scaling permissions.
- If instance deployment fails and `eb-engine.log` shows missing `.env.db` or
  `.env.api`, exclude `docker-compose.yml` from EB by adding `.ebignore` so EB
  uses the `Dockerfile` instead.
- Also exclude local caches and SSH keys in `.ebignore` (example: `.venv/`,
  `.mypy_cache/`, `.ruff_cache/`, `__pycache__/`, and `*.pem`).
- If `eb deploy` fails while creating the zip with a `.venv/bin/python3` error,
  move or remove the local `.venv` directory before deploying.
- If Docker build fails because `/tests` is missing, remove `COPY tests` and
  `COPY pytest.ini` from `Dockerfile` or include tests in the bundle.

### Step 6: Run database migrations
1. SSH into the EB instance: `eb ssh`.
2. List running containers: `sudo docker ps`.
3. Run migrations:
   - `sudo docker exec <container_id> alembic upgrade head`

### Step 7: Verify it works
1. Open in a browser:
   - `https://<eb-url>/health`
   - `https://<eb-url>/db/health`
2. Optional smoke test (requires CLI):
   - `BASE_URL=https://<eb-url> API_TOKEN=... bash scripts/e2e_smoke.sh`

## Reproducibility checklist (record these)
- AWS region and account ID.
- EB application name and environment name.
- EB platform (Docker) and instance type.
- VPC ID, subnets, and EB/RDS security group IDs.
- RDS instance identifier, engine version, and instance class.
- RDS endpoint, port, database name, and master username.
- `DATABASE_URL` and whether SSL/TLS is required.
- Whether admin UI is enabled (`ADMIN_*` values).
- Any EB hooks used for migrations.

## Official documentation references
- Elastic Beanstalk overview: https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/Welcome.html
- EB CLI setup: https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/eb-cli3.html
- Deploying Docker to EB: https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/create_deploy_docker.html
- Docker image prep (ports): https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/single-container-docker-configuration.html
- EB environment variables: https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/environments-cfg-softwaresettings.html
- EB and VPCs: https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/using-vpc.html
- RDS PostgreSQL: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html
- RDS in a VPC: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html
- Connecting to RDS PostgreSQL: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_ConnectToPostgreSQLInstance.html
- RDS SSL/TLS: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html
