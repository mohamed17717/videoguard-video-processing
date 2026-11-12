#!/usr/bin/env bash
# One-off, IDEMPOTENT bootstrap of Terraform remote state (CONTRACT.md section 10).
#
#   usage: ./bootstrap-state.sh <dev|prod>
#
# Creates:
#   1. S3 bucket vg-tfstate-<env>  - versioning ON, default encryption SSE-S3,
#      Block Public Access ON, deny non-HTTPS (same posture as the media bucket).
#   2. DynamoDB table vg-tfstate-lock (LockID string, pay-per-request,
#      server-side encrypted) - the Terraform state lock.
#
# Idempotent: every step checks for existence first, so re-running is safe.
#
# DEFERRED GATE: the `aws` CLI is NOT installed in this environment and this
# script has never been executed here. It runs once, from an operator shell
# with AWS credentials, before the first `terraform init`.
set -euo pipefail

ENVIRONMENT="${1:?usage: bootstrap-state.sh <dev|prod>}"
case "$ENVIRONMENT" in
  dev|prod) ;;
  *) echo "environment must be dev or prod" >&2; exit 2 ;;
esac

AWS_REGION="${AWS_REGION:-us-east-1}"
BUCKET="vg-tfstate-${ENVIRONMENT}"
TABLE="vg-tfstate-lock"

log() { printf '[bootstrap] %s\n' "$*"; }

command -v aws >/dev/null 2>&1 || { echo "aws CLI not found; install it first" >&2; exit 1; }

# ---------------------------------------------------------------- bucket --
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    log "bucket ${BUCKET} already exists"
else
    log "creating bucket ${BUCKET} in ${AWS_REGION}"
    if [ "$AWS_REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$BUCKET" >/dev/null
    else
        aws s3api create-bucket --bucket "$BUCKET" \
            --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
    fi
fi

log "enabling versioning + encryption + Block Public Access on ${BUCKET}"
aws s3api put-bucket-versioning --bucket "$BUCKET" \
    --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Deny non-HTTPS access to the state bucket (same posture as the media bucket).
POLICY=$(cat <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyInsecureTransport",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": ["arn:aws:s3:::BUCKETNAME", "arn:aws:s3:::BUCKETNAME/*"],
    "Condition": { "Bool": { "aws:SecureTransport": "false" } }
  }]
}
JSON
)
POLICY="${POLICY//BUCKETNAME/$BUCKET}"
if aws s3api get-bucket-policy --bucket "$BUCKET" >/dev/null 2>&1; then
    log "bucket policy already set on ${BUCKET}"
else
    log "attaching deny-insecure-transport policy to ${BUCKET}"
    printf '%s' "$POLICY" | aws s3api put-bucket-policy --bucket "$BUCKET" --policy file:///dev/stdin
fi

# ------------------------------------------------------------ lock table --
if aws dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1; then
    log "lock table ${TABLE} already exists"
else
    log "creating lock table ${TABLE}"
    aws dynamodb create-table --table-name "$TABLE" \
        --attribute-name LockID --attribute-type S \
        --billing-mode PAY_PER_REQUEST \
        --sse-specification Enabled=true >/dev/null
    aws dynamodb wait table-exists --table-name "$TABLE"
fi

log "done. Next steps:"
log "  1. infra/backend.tf: replace REPLACE_WITH_ENV with ${ENVIRONMENT}"
log "     (or run: terraform init -backend-config=\"bucket=${BUCKET}\")"
log "  2. cd infra && terraform init"
log "  3. cp terraform.tfvars.example terraform.tfvars && fill in real values"
log "  4. terraform plan   # review, then apply in CI (gated)"
