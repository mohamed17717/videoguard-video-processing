# Packer variables for the worker AMI (all defaulted, so `packer validate`
# runs without -var flags; a real build overrides pins via -var or a var file).
#
# DEFERRED GATE (CONTRACT.md section 11): `packer validate` / `packer build`
# run in CI (.github/workflows/aws-pipeline.yml, job "packer", gated on the
# production environment). No Packer binary exists in this environment; these
# files are only parsed with python-hcl2.

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  # The build MUST run on the target GPU class so `nvidia-smi` can be
  # asserted against a real L4.
  type    = string
  default = "g6.xlarge"
}

variable "ami_name_prefix" {
  type    = string
  default = "vg-worker"
}

variable "source_ami_name" {
  # Ubuntu 22.04 (jammy) amd64 server images from Canonical (099720109477).
  type    = string
  default = "ubuntu/images/*ubuntu-jammy-22.04-amd64-server-*"
}

variable "source_ami_owner" {
  type    = string
  default = "099720109477"
}

variable "ssh_username" {
  type    = string
  default = "ubuntu"
}

# ---------------------------------------------------------------- pins ----
# Every pin below carries the same rule as SECURITY.md section 1: pin by
# exact version/commit, re-pin monthly, never use latest in a tracked file.

variable "nvidia_driver_version" {
  # Minimum driver for the L4 + nv-codec-headers build (CONTRACT.md section 5).
  type    = string
  default = "550.54.14"
}

variable "nvidia_container_toolkit_version" {
  # Verify the exact deb version against the NVIDIA apt repo at build time
  # (deferred gate); the variable keeps the pin in exactly one place.
  type    = string
  default = "1.17.4-1"
}

variable "docker_version" {
  # docker-ce from Docker's official jammy apt repo. Verify at build time.
  type    = string
  default = "5:27.3.1-1~ubuntu.22.04~jammy"
}

variable "cloudwatch_agent_version" {
  # DEFERRED: default is the unfixed "latest" S3 path; a real build MUST
  # replace it with the exact pinned version directory after verifying the
  # download URL layout (SECURITY.md section 1: pinning is not optional).
  type    = string
  default = "latest"
}

variable "shaka_version" {
  # Shaka Packager release tag (v-prefixed on GitHub, tag without prefix here).
  type    = string
  default = "3.2.0"
}

variable "ffmpeg_commit" {
  # The SAME pinned FFmpeg commit as the generator image and the validator
  # image (SECURITY.md section 1). The second FFmpeg build below enables
  # libx264 + h264_nvenc, which the generator image deliberately lacks.
  type    = string
  default = "3a0867c2bfda4a4d4309ca1a8cbdc6175e67f587"
}

variable "nv_codec_commit" {
  # nv-codec-headers pin, same as the generator image Dockerfile.
  type    = string
  default = "c69278340ab1d5559c7d7bf0edf615dc33ddbba7"
}

variable "pyenv_version" {
  # pyenv release tag used to build the pinned Python.
  type    = string
  default = "v2.4.14"
}

variable "python_version" {
  # Matches deploy/worker/install.sh (PYTHON_BIN default) and CONTRACT.md 10.
  type    = string
  default = "3.12.0"
}

# ------------------------------------------------------------- images -----
# Full ECR references for the pre-pull step, e.g.
#   <account>.dkr.ecr.us-east-1.amazonaws.com/forensic-wm-generator:hevc-v1-<sha>
# Empty = skip the pre-pull (the first job then pulls; acceptable for dev).

variable "generator_image" {
  type    = string
  default = ""
}

variable "validator_image" {
  type    = string
  default = ""
}
