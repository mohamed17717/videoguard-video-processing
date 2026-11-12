# ASG + launch template (CONTRACT.md sections 2 and 8).
#
# One instance handles exactly ONE SQS message, then terminates itself.
# Scale-out is driven by queue depth; scale-in NEVER happens automatically -
# self-termination is the ONLY scale-in path (CONTRACT.md section 2).

locals {
  # The ASG name is needed BOTH by the ASG resource and by the launch
  # template's user data (VG_ASG_NAME). Referencing the resource from the
  # launch template would create a dependency cycle
  # (ASG -> launch template -> ASG), so the derived name lives here and both
  # sides read the same literal.
  worker_asg_name = "vg-worker-${var.env}"
}

resource "aws_launch_template" "worker" {
  name_prefix   = "vg-worker-${var.env}-"
  description   = "video-guard ephemeral worker: one job per boot, terminate after (CONTRACT.md section 8)."
  image_id      = var.ami_id
  instance_type = var.instance_type

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  # Public IPv4 on each throwaway worker is deliberate (PRICING.md: no NAT
  # Gateway; ~$0.002 of elastic-IP time per 24-minute job). Security groups
  # cannot allowlist hostnames - see security-groups.tf for the honest
  # network-boundary statement.
  network_interfaces {
    associate_public_ip_address = true
    delete_on_termination       = true
    security_groups             = [aws_security_group.worker.id]
  }

  # IMDSv2 only: the worker reads its instance id from IMDS for
  # _LOCK writes and self-termination; tokens are required.
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  # When the OS halts, the instance TERMINATES (not stops): the instance is
  # never reused, so no state can leak between jobs (CONTRACT.md section 8).
  instance_initiated_shutdown_behavior = "terminate"

  # 100 GiB gp3 encrypted root volume, deleted on termination. /work lives on
  # the root volume; the DISK_FULL gate (2.5x source + 20 GiB) protects it.
  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # Runtime configuration for the worker agent. The AMI's boot-time loader
  # (deploy/worker/bootstrap-env.sh) reads this user data through IMDSv2 and
  # atomically writes /etc/vg/worker.env (VG_* names match CONTRACT.md
  # section 5 and configs/aws.worker.yaml) before starting
  # worker-agent.service. jsonencode keeps this file heredoc-free.
  #
  # NOTE: the shape stays base64encode(jsonencode({...})) - the user-data
  # endpoint returns this value base64-encoded, and bootstrap-env.sh decodes
  # it before parsing the JSON.
  user_data = base64encode(jsonencode({
    VG_AWS_REGION           = local.region
    VG_S3_BUCKET            = local.bucket_name
    VG_SQS_QUEUE_URL        = aws_sqs_queue.jobs.id
    VG_SQS_DLQ_URL          = aws_sqs_queue.dlq.id
    VG_WEBHOOK_URL          = local.webhook_url
    VG_WEBHOOK_SECRET_ARN   = var.webhook_secret_arn
    VG_AXINOM_SECRET_ARN    = var.axinom_secret_arn
    VG_WATERMARK_SECRET_ARN = var.watermark_secret_arn
    VG_ASG_NAME             = local.worker_asg_name
    VG_JOB_DEADLINE_SEC     = tostring(var.job_deadline_sec)
    VG_CONFIG_PATH          = "/etc/vg/pipeline.yaml"
    VG_WORK_DIR             = "/work"
    VG_GENERATOR_IMAGE      = local.generator_image
    VG_VALIDATOR_IMAGE      = local.validator_image
    VG_LOG_LEVEL            = "INFO"
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = local.worker_asg_name
    }
  }
}

resource "aws_autoscaling_group" "worker" {
  name                = local.worker_asg_name
  min_size            = var.asg_min
  max_size            = var.asg_max
  desired_capacity    = var.asg_min
  vpc_zone_identifier = data.aws_subnets.default.ids

  launch_template {
    id      = aws_launch_template.worker.id
    version = "$Latest"
  }
}

# ------------------------------------------------ scale-out only policies --
#
# min_size may be 0 (PRICING.md) and self-termination is the ONLY scale-in
# path (CONTRACT.md section 2): nothing below ever removes capacity.
#
# Two policies are needed because one cannot do both jobs safely:
#
#   1. zero -> one: a step-scaling alarm on the RAW visible count. A target
#      tracking policy with target 1 would not react to one waiting message
#      (metric == target), and there is no per-instance denominator at zero.
#   2. sustained backlog: target tracking on a CAPACITY-AWARE metric
#      (visible messages per in-service instance). Raw depth is wrong for
#      this: 10 waiting messages is 1-per-worker on a 10-worker fleet and
#      10-per-worker on a 1-worker fleet.
#
# Double-scale risk: both policies can add capacity for the same waiting
# message. It is bounded by the step policy's 300 s instance warmup (it adds
# +1 only on an alarm transition), the target tracking policy's 300 s
# estimated_instance_warmup (a new instance does not bias the per-instance
# metric until it is warm) and var.asg_max. The ASG can never exceed
# max_size, and any over-provisioned worker drains the queue and then
# self-terminates after one message.

# 1. Zero -> one. One message is enough: the alarm fires at >= 1 and the
# step policy adds exactly ONE instance. The 300 s cooldown guarantees at
# most one launch per window even while the alarm stays ALARM.
resource "aws_cloudwatch_metric_alarm" "queue_waiting_scale_out" {
  alarm_name          = "vg-${var.env}-queue-waiting-scale-out"
  alarm_description   = "At least one job is waiting in the jobs queue: add one worker (scale-out only)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = local.queue_name
  }

  alarm_actions = [aws_autoscaling_policy.scale_out_from_zero.arn]
}

resource "aws_autoscaling_policy" "scale_out_from_zero" {
  name                    = "vg-worker-scale-out-from-zero-${var.env}"
  autoscaling_group_name  = aws_autoscaling_group.worker.name
  policy_type             = "StepScaling"
  adjustment_type         = "ChangeInCapacity"
  metric_aggregation_type = "Maximum"

  # EC2 Auto Scaling does not accept the SimpleScaling `cooldown` field on a
  # step policy (the AWS provider rejects cooldown != 0 for StepScaling).
  # The 300 s throttle is expressed with estimated_instance_warmup instead:
  # an instance launched by this policy is not warm for 300 s, matching the
  # boot budget, and the alarm action only fires on an OK -> ALARM
  # transition, so this policy adds +1 once per transition. Sustained
  # backlog belongs to the target tracking policy below.
  estimated_instance_warmup = 300

  # Lower bound 0 = "any breach of the >= 1 alarm" (the step interval is
  # metric minus threshold, and the lower bound is inclusive). The upper
  # bound is omitted, which AWS treats as positive infinity. A step
  # adjustment may not have BOTH bounds null, so the lower bound is required.
  step_adjustment {
    metric_interval_lower_bound = 0
    scaling_adjustment          = 1
  }
}

# 2. Sustained backlog, capacity-aware. The metric math divides the queue by
# the current in-service fleet; IF() falls back to the RAW count when the
# fleet is at zero, so the denominator can never be 0 and a cold ASG still
# requests capacity. target_value = 1 means "one waiting job per worker".
#
# disable_scale_in = true: ApproximateNumberOfMessagesVisible does NOT count
# a message currently being processed (it is invisible), so scaling in on it
# would kill a busy worker mid-job. Self-termination stays the ONLY scale-in
# path (CONTRACT.md section 2).
resource "aws_autoscaling_policy" "scale_out_on_backlog" {
  name                      = "vg-worker-scale-out-backlog-${var.env}"
  autoscaling_group_name    = aws_autoscaling_group.worker.name
  policy_type               = "TargetTrackingScaling"
  estimated_instance_warmup = 300

  target_tracking_configuration {
    disable_scale_in = true
    target_value     = 1

    customized_metric_specification {
      metrics {
        id          = "visible"
        label       = "SQS visible messages"
        return_data = false
        metric_stat {
          metric {
            namespace   = "AWS/SQS"
            metric_name = "ApproximateNumberOfMessagesVisible"
            dimensions {
              name  = "QueueName"
              value = local.queue_name
            }
          }
          stat   = "Average"
          period = 60
        }
      }

      metrics {
        id          = "inservice"
        label       = "ASG in-service instances"
        return_data = false
        metric_stat {
          metric {
            namespace   = "AWS/AutoScaling"
            metric_name = "GroupInServiceInstances"
            dimensions {
              name  = "AutoScalingGroupName"
              value = aws_autoscaling_group.worker.name
            }
          }
          stat   = "Average"
          period = 60
        }
      }

      metrics {
        id          = "backlog_per_instance"
        label       = "SQS visible messages per in-service worker (raw when idle)"
        expression  = "IF(inservice > 0, visible / inservice, visible)"
        return_data = true
      }
    }
  }
}
