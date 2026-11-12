"""Shared paths for the infrastructure tests.

This file is tests/infra/conftest.py in the standalone
video-processing repo:
    parents[0] = tests/infra
    parents[1] = tests
    parents[2] = the repository root (video-processing)

This repo is checked out as the `video-processing` submodule of `video-guard`,
but every path below resolves against THIS repo root: infra/, edge/, deploy/,
configs/ and .github/ all live at parents[2]. Both VIDEO_PROCESSING_ROOT
(the pipeline/worker tree) and REPO_ROOT therefore resolve to parents[2].
"""

from __future__ import annotations

from pathlib import Path

VIDEO_PROCESSING_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]

INFRA_DIR = VIDEO_PROCESSING_ROOT / "infra"
PACKER_DIR = VIDEO_PROCESSING_ROOT / "deploy" / "packer"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "aws-pipeline.yml"
# The built Bunny-runtime bundle (see edge/bunny/README.md).
EDGE_BUNDLE_PATH = VIDEO_PROCESSING_ROOT / "edge" / "bunny" / "deploy" / "entry.js"

# Files that must parse with python-hcl2. terraform.tfvars.example is HCL
# too (it is a var file), so it is parsed as well.
TERRAFORM_FILENAMES = [
    "providers.tf",
    "backend.tf",
    "variables.tf",
    "data.tf",
    "s3.tf",
    "sqs.tf",
    "iam.tf",
    "ecr.tf",
    "asg.tf",
    "security-groups.tf",
    "cloudwatch.tf",
    "bunny.tf",
    "cloudflare.tf",
    "outputs.tf",
]
PACKER_FILENAMES = ["worker.pkr.hcl", "variables.pkr.hcl"]

# Every CI workflow file, for the structural GitHub Actions checks. In this
# standalone repo there is a single workflow: .github/workflows/aws-pipeline.yml.
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_PATHS = sorted(WORKFLOW_DIR.glob("*.yml"))
