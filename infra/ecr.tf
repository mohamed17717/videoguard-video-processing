# ECR: the two pinned images (CONTRACT.md sections 0 and 10).
#
#   forensic-wm-generator - built ONLY from the forensic-wm/ directory context
#     (docker build ... forensic-wm, CONTRACT.md section 0). It is a NESTED
#     git submodule of this repo (public HTTPS, pinned in `.gitmodules`).
#   vg-validator - the ffprobe + ClamAV validation image
#     (deploy/validator/Dockerfile).
#
# Immutable tags + scan on push: a tag can never be silently replaced, and
# every push is scanned (basic scanning is free; findings are a rebuild
# trigger per SECURITY.md section 1).

resource "aws_ecr_repository" "generator" {
  name                 = "forensic-wm-generator"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "validator" {
  name                 = "vg-validator"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Lifecycle: keep the 10 most recent images per repository. The worker pins an
# exact image reference in user_data, so old images must survive a few
# generations for requeue/reconciliation - but not forever.
resource "aws_ecr_lifecycle_policy" "generator" {
  repository = aws_ecr_repository.generator.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the 10 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}

resource "aws_ecr_lifecycle_policy" "validator" {
  repository = aws_ecr_repository.validator.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the 10 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}
