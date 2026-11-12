# SQS dispatch queue + DLQ (CONTRACT.md section 2).
#
# SQS is used for DISPATCH ONLY. Progress travels over short HMAC-signed
# webhooks to Railway (CONTRACT.md section 3). The worker processes exactly
# ONE message per instance and terminates itself.

# Dead-letter queue: 14 days retention so a dead job can be inspected calmly.
resource "aws_sqs_queue" "dlq" {
  name                       = local.dlq_name
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 1209600
}

# Jobs queue: 4 days retention (CONTRACT.md section 2), visibility 900 s
# (refreshed by the 300 s worker heartbeat).
resource "aws_sqs_queue" "jobs" {
  name                       = local.queue_name
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 345600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })

  depends_on = [aws_sqs_queue.dlq]
}

# Queue policy: only THIS bucket in THIS account may send messages.
# The two conditions (SourceArn + SourceAccount) are the standard anti-squat
# pair; without them any AWS account could push forged job events.
data "aws_iam_policy_document" "jobs_queue_policy" {
  statement {
    sid       = "AllowMediaBucketToSendJobEvents"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:s3:::${local.bucket_name}"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "jobs" {
  queue_url = aws_sqs_queue.jobs.id
  policy    = data.aws_iam_policy_document.jobs_queue_policy.json
}
