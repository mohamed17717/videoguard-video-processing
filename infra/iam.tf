# IAM: three separate identities (CONTRACT.md sections 1 and 8).
#
#   1. worker     - EC2 instance profile for the ephemeral g6.xlarge.
#   2. backend    - IAM user for the Railway control plane (presign + sidecar
#                   writes + SQS dispatch). Access key stored in Railway
#                   secrets, rotated; output is sensitive.
#   3. bunny_origin - read-only identity Bunny uses for AWSSigningEnabled
#                   origin pulls. s3:GetObject on renditions/ ONLY.
#
# NOTE ON PER-JOB SCOPING: CONTRACT.md section 8 writes permissions as
# `incoming/<job>/*` (one job per prefix). A static instance-profile policy
# cannot carry a runtime value (job_id is a UUID created at upload time), so
# the static grant covers the WHOLE prefix under each class. Job isolation is
# enforced by the lifecycle instead: one instance handles exactly one message,
# the instance terminates after it, and the _LOCK conditional write (CONTRACT.md
# section 2) prevents two instances on the same job. Narrowing to per-job
# scopes (e.g. a bootstrap-time session policy) is a deferred hardening step -
# document any change in infra/README.md.

# ----------------------------------------------------------------- worker --
data "aws_iam_policy_document" "worker_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "vg-worker-${var.env}"
  assume_role_policy = data.aws_iam_policy_document.worker_assume_role.json
}

data "aws_iam_policy_document" "worker" {
  # Read: the raw upload, the job sidecar, the reports (for _LOCK on 412 and
  # _DONE on redelivery) and renditions (HeadObject before rendition_ready).
  statement {
    sid     = "ReadJobArtifacts"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/incoming/*",
      "arn:aws:s3:::${local.bucket_name}/jobs/*",
      "arn:aws:s3:::${local.bucket_name}/reports/*",
      "arn:aws:s3:::${local.bucket_name}/renditions/*",
    ]
  }

  # Write: masters, renditions, reports. NEVER incoming/ (explicit Deny below;
  # CONTRACT.md section 2 - the worker never writes to incoming/).
  statement {
    sid     = "WriteJobOutputs"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/masters/*",
      "arn:aws:s3:::${local.bucket_name}/renditions/*",
      "arn:aws:s3:::${local.bucket_name}/reports/*",
    ]
  }

  # HeadObject on a MISSING key: S3 returns 403 (not 404) when the caller
  # lacks s3:ListBucket, and the worker treats only 404 as "absent"
  # (src/vg_worker/s3io.py::done_exists). Without this grant, a first run
  # whose reports/_DONE does not exist yet fails with 403 instead of
  # proceeding. ListBucket is a BUCKET-level action, so the resource is the
  # bucket and the s3:prefix condition scopes it to the prefixes the worker
  # HEADs: reports/_DONE, reports/_LOCK (redelivery check) and the uploaded
  # rendition segments.
  statement {
    sid       = "ListHeadObjectPrefixes"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.bucket_name}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "reports/*",
        "renditions/*",
      ]
    }
  }

  # Tagging: outputs plus the ORIGINAL (tag processed=true for archive and
  # vg-retention for the lifecycle rules). Tagging is a separate action from
  # PutObject, so the Deny on incoming/ does not block it.
  statement {
    sid     = "TagJobArtifacts"
    effect  = "Allow"
    actions = ["s3:PutObjectTagging"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/masters/*",
      "arn:aws:s3:::${local.bucket_name}/renditions/*",
      "arn:aws:s3:::${local.bucket_name}/reports/*",
      "arn:aws:s3:::${local.bucket_name}/incoming/*",
    ]
  }

  # Explicit Deny: the worker must never write to incoming/. This is the
  # loop guard for the S3 event notification (CONTRACT.md section 2).
  statement {
    sid     = "DenyPutObjectOnIncoming"
    effect  = "Deny"
    actions = ["s3:PutObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/incoming/*",
    ]
  }

  # SQS: consume exactly one queue; send deterministic failures to the DLQ
  # (CONTRACT.md section 2). GetAuthorizationToken note: ECR token fetch is
  # account-scoped in AWS IAM, so the image-pull statements below are split.
  statement {
    sid    = "ConsumeJobsQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid       = "SendDeterministicFailuresToDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }

  # Secrets Manager: three ARNs, read at job time, held in memory only
  # (CONTRACT.md section 5). Empty ARNs fail at apply - they must be set.
  statement {
    sid     = "ReadPipelineSecrets"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      var.webhook_secret_arn,
      var.axinom_secret_arn,
      var.watermark_secret_arn,
    ]
  }

  # Self-termination (CONTRACT.md section 8): the ONLY scale-in path of the
  # ASG. The resource must be "*" - autoscaling API scoping on ARNs is not
  # supported for this action. --no-should-decrement-desired-capacity is
  # forbidden by the worker code, not by IAM.
  statement {
    sid       = "TerminateOwnInstance"
    effect    = "Allow"
    actions   = ["autoscaling:TerminateInstanceInAutoScalingGroup"]
    resources = ["*"]
  }

  # ECR: token fetch is account-wide by design; image reads are scoped to the
  # two pinned repositories only.
  statement {
    sid       = "EcrAuthorizationToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrReadPinnedImages"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      aws_ecr_repository.generator.arn,
      aws_ecr_repository.validator.arn,
    ]
  }

  # CloudWatch Logs: the three log groups in cloudwatch.tf.
  statement {
    sid    = "WriteWorkerLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      aws_cloudwatch_log_group.worker_supervisor.arn,
      aws_cloudwatch_log_group.worker_container.arn,
      aws_cloudwatch_log_group.worker_ffmpeg.arn,
    ]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "vg-worker-policy-${var.env}"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "vg-worker-${var.env}"
  role = aws_iam_role.worker.name
}

# ---------------------------------------------------------------- backend --
# Railway sits OUTSIDE AWS. It writes the job sidecar BEFORE returning the
# presigned URL (CONTRACT.md section 1), presigns the browser PUT onto
# incoming/<job>/ with its own credentials, and publishes JobMessages to the
# jobs queue. It also needs three READ grants (DeepSeek audit fix) because
# the Go control plane re-reads what it wrote:
#   - s3:GetObject on jobs/* - the webhook handler resolves video_public_id
#     by reading the sidecar (internal/awscontrol/webhook_handler.go,
#     SidecarVideoResolver);
#   - s3:GetObject on reports/* and renditions/* - reconciliation rebuilds
#     status from S3 when every webhook failed
#     (internal/awscontrol/reconcile.go, ListStuckProcessing);
#   - s3:ListBucket scoped to the reports/* and renditions/* prefixes -
#     reconcile's listKeys calls ListObjectsV2, a BUCKET-level action, so
#     the resource is the bucket itself and the s3:prefix condition carries
#     the scope.
# Still nothing else: no deletes, no storage-class signing, no writes beyond
# jobs/* and incoming/*.
resource "aws_iam_user" "backend" {
  name = var.railway_iam_user_name
}

data "aws_iam_policy_document" "backend" {
  statement {
    sid     = "WriteSidecarAndUploadTarget"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/jobs/*",
      "arn:aws:s3:::${local.bucket_name}/incoming/*",
    ]
  }

  # Audit fix: the webhook handler reads back the sidecar the backend wrote.
  statement {
    sid     = "ReadJobSidecar"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/jobs/*",
    ]
  }

  # Audit fix: reconciliation lists and reads reports/ + renditions/ to
  # rebuild status from S3 when every webhook failed.
  statement {
    sid     = "ReadReconcileArtifacts"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/reports/*",
      "arn:aws:s3:::${local.bucket_name}/renditions/*",
    ]
  }

  # B8: CleanupAbandonedUploads calls HeadObject on
  # incoming/<job_id>/source.mp4 to tell a genuinely absent (404) upload from
  # a successful PUT whose upload-complete ack was lost. HeadObject requires
  # s3:GetObject on incoming/*.
  statement {
    sid     = "ReadIncomingUploadForCleanup"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/incoming/*",
    ]
  }

  # Audit fix: ListObjectsV2 is a bucket-level action, so the resource is
  # the bucket; the s3:prefix condition scopes it to the prefixes the
  # reconciler lists AND incoming/, where CleanupAbandonedUploads HeadObjects
  # a possibly-missing source object (403 otherwise, never a 404).
  statement {
    sid       = "ListReconcilePrefixes"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.bucket_name}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "incoming/*",
        "reports/*",
        "renditions/*",
      ]
    }
  }

  statement {
    sid       = "DispatchJobMessages"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_user_policy" "backend" {
  name   = "vg-backend-railway-policy-${var.env}"
  user   = aws_iam_user.backend.name
  policy = data.aws_iam_policy_document.backend.json
}

# Static access key. The secret is a sensitive output: store it in Railway
# secrets, rotate it on a schedule, and never commit it anywhere.
resource "aws_iam_access_key" "backend" {
  user = aws_iam_user.backend.name
}

# ----------------------------------------------------------- bunny origin --
# Read-only identity for Bunny's AWSSigningEnabled origin pulls. Only
# renditions/ is CDN-readable (CONTRACT.md section 1); the original upload,
# the masters, the reports and the sidecar are unreachable through the pull
# zone even if a URL is guessed.
resource "aws_iam_user" "bunny_origin" {
  name = "vg-bunny-origin-${var.env}"
}

data "aws_iam_policy_document" "bunny_origin" {
  statement {
    sid     = "ReadRenditionsOnly"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}/renditions/*",
    ]
  }
}

resource "aws_iam_user_policy" "bunny_origin" {
  name   = "vg-bunny-origin-policy-${var.env}"
  user   = aws_iam_user.bunny_origin.name
  policy = data.aws_iam_policy_document.bunny_origin.json
}

resource "aws_iam_access_key" "bunny_origin" {
  user = aws_iam_user.bunny_origin.name
}
