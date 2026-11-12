# Terraform providers for the AWS video pipeline (CONTRACT.md section 0).
#
# Why three providers:
#   - hashicorp/aws: S3, SQS, IAM, ECR, ASG, CloudWatch (everything in this
#     directory except the two files below).
#   - cloudflare/cloudflare: the DNS CNAME that points the playback hostname
#     at the Bunny pull zone (cloudflare.tf).
#   - hashicorp/null: local-exec runners that call the Bunny API
#     (bunny.tf). There is no community Bunny provider we can trust to manage
#     BOTH pull zones AND edge scripts, so those two objects are managed
#     through null_resource + curl with a sensitive `bunny_api_key` variable.
#     See bunny.tf and infra/README.md for the exact trade-off.
#
# DEFERRED GATE (CONTRACT.md section 11): `terraform validate` / `plan` /
# `apply` run in CI or a real account. Locally these files are only parsed
# with python-hcl2 (tests/infra/test_hcl_parse.py).

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "video-guard"
      Component   = "video-pipeline"
      Environment = var.env
      ManagedBy   = "terraform"
    }
  }
}

provider "cloudflare" {
  # Sensitive variable (variables.tf). Never a hard-coded token.
  api_token = var.cloudflare_api_token
}
