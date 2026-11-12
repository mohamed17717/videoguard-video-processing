# CloudWatch logs + alarms (CONTRACT.md sections 8 and 10).
#
# Log groups: supervisor, container, ffmpeg stderr - three separate streams
# so a forensic question ("what did the generator print?") has one place to
# look. The worker logs instance id, AMI id, image digest and job id
# together (CONTRACT.md section 8). No secrets in any log line
# (SECURITY.md section 10).

resource "aws_cloudwatch_log_group" "worker_supervisor" {
  name              = "/vg/worker/${var.env}/supervisor"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "worker_container" {
  name              = "/vg/worker/${var.env}/container"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "worker_ffmpeg" {
  name              = "/vg/worker/${var.env}/ffmpeg"
  retention_in_days = 30
}

# ----------------------------------------------------------------- alarms --
# DLQ depth > 0 is the hard failure signal: a deterministic failure went to
# the dead-letter queue (or a transient one exhausted 3 receives). This
# metric is REAL - AWS/SQS publishes it, no worker changes needed.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "vg-${var.env}-dlq-not-empty"
  alarm_description   = "A job ended in the dead-letter queue. Inspect the DLQ message + /vg/worker logs (CONTRACT.md section 2)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 60
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = local.dlq_name
  }
}

# PLACEHOLDER ALARMS (CONTRACT.md section 10). The worker agent must publish
# these custom metrics in the VG/Worker namespace; until that code exists the
# alarms can be created but stay silent (treat_missing_data = notBreaching).
# Publishing them is a deferred gate like everything in CONTRACT.md
# section 11.

# Job duration over the deadline: the worker enforces 3600 s itself; this
# catches jobs that ran long AND succeeded, so cost can be re-profiled.
resource "aws_cloudwatch_metric_alarm" "job_duration_over_deadline" {
  alarm_name          = "vg-${var.env}-job-duration-over-deadline"
  alarm_description   = "PLACEHOLDER: needs VG/Worker JobDurationSeconds published by the worker agent. Threshold = job deadline + 10 min grace."
  namespace           = "VG/Worker"
  metric_name         = "JobDurationSeconds"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 4200
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.worker.name
  }
}

# Non-zero container exits: validator/generator/packager crashed. Any single
# event is worth a look (fail-closed design means exit != 0 must be rare).
resource "aws_cloudwatch_metric_alarm" "worker_non_zero_exit" {
  alarm_name          = "vg-${var.env}-worker-non-zero-exit"
  alarm_description   = "PLACEHOLDER: needs VG/Worker ContainerNonZeroExit published by the worker agent."
  namespace           = "VG/Worker"
  metric_name         = "ContainerNonZeroExit"
  statistic           = "Sum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.worker.name
  }
}

# Webhook failure rate: the worker retries 3x with backoff and continues
# (fire-and-forget), so silent webhook loss must be visible from AWS side.
resource "aws_cloudwatch_metric_alarm" "webhook_failures" {
  alarm_name          = "vg-${var.env}-webhook-failures"
  alarm_description   = "PLACEHOLDER: needs VG/Worker WebhookFailures published by the worker agent. The backend reconciles from reports/ (CONTRACT.md section 3)."
  namespace           = "VG/Worker"
  metric_name         = "WebhookFailures"
  statistic           = "Sum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.worker.name
  }
}

# Unusual worker egress: AWS/EC2 publishes NetworkOut per instance AND as an
# aggregate with the AutoScalingGroupName dimension. A normal job uploads
# ~10 GB total; this fires far above any plausible transfer, catching e.g. a
# compromised worker exfiltrating data. Threshold = 100 GiB per 5-minute
# period; tune after the first real bills.
resource "aws_cloudwatch_metric_alarm" "worker_egress_anomaly" {
  alarm_name          = "vg-${var.env}-worker-egress-anomaly"
  alarm_description   = "Aggregate instance NetworkOut far above a normal job (~10 GB). Real AWS/EC2 metric; tune the threshold on real data."
  namespace           = "AWS/EC2"
  metric_name         = "NetworkOut"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 107374182400
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.worker.name
  }
}
