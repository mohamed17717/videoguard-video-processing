# The media bucket (CONTRACT.md section 1).
#
# Key classes:
#   jobs/, incoming/, masters/, reports/  -> never CDN-readable
#   renditions/                           -> the ONLY prefix the Bunny origin
#                                            identity may read (iam.tf, policy
#                                            "BunnyOriginReadOnly"). Thumbnails
#                                            live under renditions/<job>/thumbs/
#                                            for exactly that reason.
#
# The S3 event -> SQS wiring lives at the bottom of this file and depends on
# the queue policy in sqs.tf.

resource "aws_s3_bucket" "media" {
  bucket = local.bucket_name
}

# Block Public Access: all four flags ON. Nothing in this bucket is public;
# delivery goes through Bunny with its own AWS-signed origin identity.
resource "aws_s3_bucket_public_access_block" "media" {
  bucket = aws_s3_bucket.media.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# BucketOwnerEnforced: ACLs are disabled bucket-wide (CONTRACT.md section 9,
# "no ACLs"). Presigned PUTs never carry an ACL header.
resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Versioning DISABLED (CONTRACT.md section 1): deleted TTL titles must stop
# billing immediately, and overwrite-by-PUT (job retries) must not pile up
# noncurrent versions.
resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id

  versioning_configuration {
    status = "Disabled"
  }
}

# SSE-S3: every object encrypted at rest with an AWS-managed key. (SSE-KMS is
# the alternative; SSE-S3 is chosen for $0 and no key policy to manage.)
resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Deny any request that is not HTTPS. Simple, explicit, no exceptions.
resource "aws_s3_bucket_policy" "media" {
  bucket = aws_s3_bucket.media.id
  policy = data.aws_iam_policy_document.media_bucket_policy.json
}

data "aws_iam_policy_document" "media_bucket_policy" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::${local.bucket_name}",
      "arn:aws:s3:::${local.bucket_name}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# Bucket CORS for the browser upload (CONTRACT.md section 9): exactly one PUT
# rule. The presigner signs Content-Type, so the browser only needs
# content-type as an allowed header; ETag is exposed so the browser can
# confirm the upload integrity.
resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  cors_rule {
    allowed_methods = ["PUT"]
    allowed_origins = ["*"]
    allowed_headers = ["content-type"]
    expose_headers  = ["ETag"]
  }
}

# Lifecycle rules (CONTRACT.md section 1, retention):
#   incoming/: the ORIGINAL is tagged BOTH vg-retention=archive AND
#     processed=true after success (CONTRACT.md section 8 step 6), so this
#     rule keeps requiring both tags.
#   masters/ + renditions/: the worker tags OUTPUTS with vg-retention ONLY
#     (CONTRACT.md section 1: "Every output object is tagged
#     vg-retention=<retention>"); processed=true is reserved for the
#     original in incoming/. A filter requiring both tags on outputs never
#     matched anything (DeepSeek audit fix), so these rules filter on
#     vg-retention=archive alone.
#   retention=ttl30 -> expire at 30 days in STANDARD. NO Glacier transition:
#     the 90-day minimum would outlive the 30-day TTL (PRICING.md).
# DEFERRED GATE (CONTRACT.md section 11): verify against a real bucket that
#   (a) days = 0 transitions are accepted for GLACIER_IR,
#   (b) the tag+prefix filter matches the objects the worker tags,
#   (c) Glacier IR's 128 KiB minimum object size and 90-day minimum storage
#       behave as modelled with ~14,400 tiny CMAF segments.
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    id     = "archive-incoming"
    status = "Enabled"
    filter {
      and {
        prefix = "incoming/"
        tags = {
          vg-retention = "archive"
          processed    = "true"
        }
      }
    }
    transition {
      storage_class = "GLACIER_IR"
      days          = 0
    }
  }

  rule {
    id     = "archive-masters"
    status = "Enabled"
    # vg-retention ONLY (audit fix): masters are tagged vg-retention=<retention>;
    # processed=true exists only on the original, so requiring it here made
    # this rule dead.
    filter {
      and {
        prefix = "masters/"
        tags = {
          vg-retention = "archive"
        }
      }
    }
    transition {
      storage_class = "GLACIER_IR"
      days          = 0
    }
  }

  # Belt-and-braces for renditions: the worker already uploads them with
  # storage class GLACIER_IR when retention=archive; the rule keeps that true
  # even for objects uploaded through another path. vg-retention ONLY (audit
  # fix): renditions never get processed=true, only the original does.
  rule {
    id     = "archive-renditions"
    status = "Enabled"
    filter {
      and {
        prefix = "renditions/"
        tags = {
          vg-retention = "archive"
        }
      }
    }
    transition {
      storage_class = "GLACIER_IR"
      days          = 0
    }
  }

  rule {
    id     = "ttl30-expire"
    status = "Enabled"
    filter {
      and {
        tags = {
          vg-retention = "ttl30"
        }
      }
    }
    expiration {
      days = 30
    }
  }
}

# S3 event -> SQS dispatch (CONTRACT.md section 2):
#   - event type s3:ObjectCreated:Put, NEVER s3:ObjectCreated:*.
#     LOOP GUARD: a CopyObject into incoming/ fires ObjectCreated:Copy; with
#     "*" the worker's own requeue/reconciliation writes could re-trigger the
#     queue and loop. "Put" only fires on real browser uploads.
#   - prefix incoming/, suffix .mp4.
#   - s3:TestEvent is ignored by the worker (CONTRACT.md section 2).
# The notification PUT fails if the queue has no policy yet, so this waits
# for aws_sqs_queue_policy.jobs.
resource "aws_s3_bucket_notification" "media" {
  bucket = aws_s3_bucket.media.id

  queue {
    queue_arn     = aws_sqs_queue.jobs.arn
    events        = ["s3:ObjectCreated:Put"]
    filter_prefix = "incoming/"
    filter_suffix = ".mp4"
  }

  depends_on = [aws_sqs_queue_policy.jobs]
}
