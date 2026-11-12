# Outputs for the operator and for the deploy documents.
#
# The two access-key secrets are SENSITIVE: they appear only in
# `terraform output -json` / `terraform output <name>` runs by the operator,
# never in plan output. Rotation is documented in infra/README.md.

output "bucket_name" {
  description = "Media bucket (CONTRACT.md section 1: vg-media-<env>)."
  value       = aws_s3_bucket.media.bucket
}

output "bucket_arn" {
  description = "Media bucket ARN."
  value       = aws_s3_bucket.media.arn
}

output "jobs_queue_url" {
  description = "SQS jobs queue URL (VG_SQS_QUEUE_URL)."
  value       = aws_sqs_queue.jobs.id
}

output "jobs_queue_arn" {
  description = "SQS jobs queue ARN."
  value       = aws_sqs_queue.jobs.arn
}

output "dlq_queue_url" {
  description = "SQS DLQ URL (VG_SQS_DLQ_URL)."
  value       = aws_sqs_queue.dlq.id
}

output "dlq_queue_arn" {
  description = "SQS DLQ ARN (alarms + worker deterministic-failure sends)."
  value       = aws_sqs_queue.dlq.arn
}

output "ecr_registry" {
  description = "ECR registry host for this account/region."
  value       = local.ecr_registry
}

output "generator_ecr_url" {
  description = "Generator repository URL (image tag pinned in user_data)."
  value       = aws_ecr_repository.generator.repository_url
}

output "validator_ecr_url" {
  description = "Validator repository URL."
  value       = aws_ecr_repository.validator.repository_url
}

output "asg_name" {
  description = "Worker ASG name (VG_ASG_NAME, self-termination target)."
  value       = aws_autoscaling_group.worker.name
}

output "worker_role_arn" {
  description = "Worker instance-profile role ARN (least-privilege per CONTRACT.md section 8)."
  value       = aws_iam_role.worker.arn
}

output "backend_iam_user_name" {
  description = "Railway backend IAM user name."
  value       = aws_iam_user.backend.name
}

output "backend_access_key_id" {
  description = "Railway backend access key id. Store with the secret in Railway secrets."
  value       = aws_iam_access_key.backend.id
}

output "backend_access_key_secret" {
  description = "Railway backend access key SECRET. Sensitive: rotate on a schedule, never commit (infra/README.md)."
  value       = aws_iam_access_key.backend.secret
  sensitive   = true
}

output "bunny_origin_access_key_id" {
  description = "Bunny origin read-only IAM user access key id. Paste into the Bunny zone's AWS signing settings."
  value       = aws_iam_access_key.bunny_origin.id
}

output "bunny_origin_access_key_secret" {
  description = "Bunny origin read-only IAM user access key SECRET. Sensitive: same handling as backend_access_key_secret."
  value       = aws_iam_access_key.bunny_origin.secret
  sensitive   = true
}

output "pull_zone_hostname" {
  description = "Default Bunny hostname for the pull zone (the Cloudflare CNAME targets this)."
  value       = "${local.bunny_zone_name}.b-cdn.net"
}
