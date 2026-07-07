# -----------------------------------------------------------------------------
# main.tf — providers and global config for the texet perf environment.
#
# This is a disposable, isolated load-testing environment: its own VPC, its own
# RDS, ECS Fargate api + worker services, all mocked (MOCK_EXTERNAL_APIS=true).
# It shares nothing with texet production (twilio-texet/bot-prod).
#
# State is local by default (this stack is meant to be created, tested, and
# destroyed in a session). To share/persist state, add an S3 backend block and
# re-run `terraform init -migrate-state` — see README.md.
# -----------------------------------------------------------------------------

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}
