# Terraform variables for the AWS video pipeline.
#
# Rules (CONTRACT.md section 10):
#   - No secrets in tracked files: secret VALUES live in terraform.tfvars
#     (git-ignored) or environment variables; the two credential variables
#     below are marked sensitive = true.
#   - Sensitive VALUES: bunny_api_key, cloudflare_api_token. The edge token
#     secret and origin host are GitHub vars/secrets only (CI job `edge`),
#     NOT Terraform variables - they are deliberately absent here so they
#     can never land in tfvars or Terraform state (D2 ownership split).
#   - ARNs are not secret values, but the three Secrets Manager ARNs must be
#     provided by the operator (the secrets themselves are created outside
#     Terraform, in the AWS console or CLI).

variable "env" {
  description = "Deployment environment. Bucket and queue names are derived from it: vg-media-<env>, vg-video-jobs-<env> (CONTRACT.md section 1)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be 'dev' or 'prod' (CONTRACT.md section 1)."
  }
}

variable "region" {
  description = "AWS region. The contract fixes us-east-1 (CONTRACT.md section 1)."
  type        = string
  default     = "us-east-1"
}

variable "account_id" {
  description = "AWS account id. Leave empty in real deployments: the account id is read from data.aws_caller_identity. Only set it for offline/test plans."
  type        = string
  default     = ""
}

variable "bucket_name" {
  description = "Media bucket name. Leave empty to use the contract name vg-media-<env>."
  type        = string
  default     = ""
}

variable "webhook_host" {
  description = "Railway host for progress webhooks, e.g. 'vg-api-production.up.railway.app'. No trailing slash, no scheme."
  type        = string
}

variable "webhook_path" {
  description = "Exact route path of the pipeline webhook on the Go backend (internal/awscontrol/webhook_handler.go). Operator input on purpose: this file never invents a route."
  type        = string
  default     = ""
}

variable "pull_zone_hostname" {
  description = "Playback hostname published on Cloudflare and pointed at the Bunny pull zone, e.g. 'media' or 'media.example.com'. See cloudflare.tf."
  type        = string
}

variable "bunny_api_key" {
  description = "Bunny.net account API key, used ONLY by the local-exec curl calls in bunny.tf. Never echoed, never stored as a resource attribute."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token scoped to the target zone (DNS edit). Passed to the cloudflare provider."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone id of the domain that serves playback."
  type        = string
  default     = ""
}

variable "railway_iam_user_name" {
  description = "IAM user name for the Railway backend (presign + SQS + sidecar writes; CONTRACT.md section 1)."
  type        = string
  default     = "vg-backend-railway"
}

variable "asg_min" {
  description = "Minimum worker instances. 0 = scale from zero (PRICING.md)."
  type        = number
  default     = 0
}

variable "asg_max" {
  description = "Maximum worker instances. 3 caps the parallel-boot exposure (PRICING.md)."
  type        = number
  default     = 3
}

variable "instance_type" {
  description = "Worker instance type. g6.xlarge = L4 GPU (PRICING.md compute table)."
  type        = string
  default     = "g6.xlarge"
}

variable "ami_id" {
  description = "AMI id produced by the Packer build in deploy/packer/ (e.g. ami-0abc...). Must be set before apply; empty keeps plan working but apply fails loudly."
  type        = string
  default     = ""
}

variable "generator_image" {
  description = "Full ECR reference of the generator image, e.g. '<account>.dkr.ecr.us-east-1.amazonaws.com/forensic-wm-generator:hevc-v1-<sha>'. Empty falls back to an 'unpinned' local placeholder."
  type        = string
  default     = ""
}

variable "validator_image" {
  description = "Full ECR reference of the validator image, e.g. '<account>.dkr.ecr.us-east-1.amazonaws.com/vg-validator:pinned-<sha>'."
  type        = string
  default     = ""
}

variable "presign_ttl_seconds" {
  description = "Informational: the agreed presigned-PUT TTL (CONTRACT.md section 7: 3600 s, or 900 s for short-lived tokens; this pipeline uses 900 s). The Go backend reads it from its own config; this variable documents the agreed value so IaC review sees it."
  type        = number
  default     = 900
}

variable "job_deadline_sec" {
  description = "Worker job deadline in seconds, enforced by the worker itself (CONTRACT.md sections 4 and 8)."
  type        = number
  default     = 3600
}

variable "visibility_timeout_seconds" {
  description = "SQS visibility timeout (CONTRACT.md section 2). The worker heartbeat refreshes it every heartbeat_sec."
  type        = number
  default     = 900
}

variable "heartbeat_sec" {
  description = "Heartbeat interval: SQS ChangeMessageVisibility + _LOCK renewal (CONTRACT.md sections 2 and 8)."
  type        = number
  default     = 300
}

# Secrets Manager ARNs. The secret VALUES are fetched by the worker at run
# time with secretsmanager:GetSecretValue and never pass through Terraform.
variable "webhook_secret_arn" {
  description = "Secrets Manager ARN of the webhook HMAC secret (JSON shape {\"secret\":\"...\"}, CONTRACT.md section 5)."
  type        = string
  default     = ""
}

variable "axinom_secret_arn" {
  description = "Secrets Manager ARN of the Axinom DRM signing material (JSON shape with signing_key_hex/signing_iv_hex/endpoint/provider_name/scheme, CONTRACT.md section 5)."
  type        = string
  default     = ""
}

variable "watermark_secret_arn" {
  description = "Secrets Manager ARN of the watermark secret (JSON shape {\"secret\":\"...\"}, CONTRACT.md section 5)."
  type        = string
  default     = ""
}
