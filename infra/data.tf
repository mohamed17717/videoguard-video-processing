# Data sources and computed locals shared by every file in infra/.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# Default-VPC subnets keep this module runnable with zero networking inputs
# (PRICING.md: no NAT Gateway, no always-on AWS networking). For a production
# VPC, replace these two data sources with explicit subnet ids and document
# the change in infra/README.md.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

locals {
  # Media bucket name follows CONTRACT.md section 1: vg-media-<env>.
  # var.bucket_name only overrides it for unusual deployments.
  bucket_name = var.bucket_name != "" ? var.bucket_name : "vg-media-${var.env}"

  # var.account_id is a fallback for tests; the real value comes from the
  # caller identity (nothing hard-coded, nothing to leak).
  account_id = var.account_id != "" ? var.account_id : data.aws_caller_identity.current.account_id

  region = var.region != "" ? var.region : data.aws_region.current.name

  queue_name = "vg-video-jobs-${var.env}"
  dlq_name   = "${local.queue_name}-dlq"

  ecr_registry = "${local.account_id}.dkr.ecr.${local.region}.amazonaws.com"

  # Railway webhook endpoint. var.webhook_path must be the EXACT route of the
  # Go webhook handler (internal/awscontrol/webhook_handler.go); it is an
  # operator input on purpose so this file never invents a route.
  webhook_url = "https://${var.webhook_host}${var.webhook_path}"

  generator_image = var.generator_image != "" ? var.generator_image : "${local.ecr_registry}/forensic-wm-generator:hevc-v1-unpinned"
  validator_image = var.validator_image != "" ? var.validator_image : "${local.ecr_registry}/vg-validator:pinned-unpinned"
}
