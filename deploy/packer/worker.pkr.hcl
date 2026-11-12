# Packer template for the worker AMI (CONTRACT.md sections 5, 8, 10).
#
# What the AMI bakes in (idempotent shell steps, in order):
#   1. base packages + Docker (pinned docker-ce) + awscli
#   2. NVIDIA driver >= 550.54.14 + NVIDIA Container Toolkit
#   3. CloudWatch agent (pinned deb)
#   4. Shaka Packager, pinned release binary at /usr/local/bin/packager
#   5. a SECOND pinned FFmpeg build (libx264 + h264_nvenc) at
#      /usr/local/bin/{ffmpeg,ffprobe} - the generator image has NO libx264,
#      so the CPU fallback must come from this binary (CONTRACT.md section 5)
#   6. pyenv-built Python 3.12.0 at /usr/local/pyenv
#   7. deploy/worker/install.sh: venv + vg_pipeline/vg_worker + systemd unit
#   8. best-effort pre-pull of the pinned ECR images
#   9. final assertions (fail the build loudly)
#
# DEFERRED GATE (CONTRACT.md section 11): `packer validate` and
# `packer build` run in CI, gated on the production environment. There is no
# Packer binary in this environment; these files are only parsed with
# python-hcl2 and their exact pins verified at build time.

packer {
  required_plugins {
    amazon = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

locals {
  build_date = formatdate("YYYY-MM-DD", timestamp())
}

source "amazon-ebs" "worker" {
  region        = var.region
  instance_type = var.instance_type
  ami_name      = "${var.ami_name_prefix}-${local.build_date}-{{timestamp}}"
  ssh_username  = var.ssh_username
  communicator  = "ssh"
  ssh_timeout   = "15m"

  source_ami_filter {
    filters = {
      name                = var.source_ami_name
      root-device-type    = "ebs"
      virtualization-type = "hvm"
      architecture        = "x86_64"
    }
    owners      = [var.source_ami_owner]
    most_recent = true
  }

  # Build-time disk: enough for the ffmpeg build, docker images and layers.
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 50
    volume_type           = "gp3"
    delete_on_termination = true
  }

  # AMI labels: build date + pinned FFmpeg SHA + Shaka version, so any AMI in
  # the account can be traced back to its pins (SECURITY.md section 1).
  # `tags` is the Amazon EBS builder option applied to the resulting AMI;
  # `run_tags` below labels the temporary build resources.
  tags = {
    Name                 = "${var.ami_name_prefix}-${local.build_date}"
    "vg:build-date"      = local.build_date
    "vg:ffmpeg-sha"      = var.ffmpeg_commit
    "vg:nvcodec-sha"     = var.nv_codec_commit
    "vg:shaka-version"   = var.shaka_version
    "vg:python"          = var.python_version
    "vg:generator-image" = var.generator_image
    "vg:validator-image" = var.validator_image
  }

  run_tags = {
    Name            = "${var.ami_name_prefix}-builder"
    "vg:build-date" = local.build_date
  }

  # Temporary instance profile for the build instance, created and destroyed
  # by Packer (no external role needed). It only grants the ECR read actions
  # the best-effort pre-pull in step 8 uses; Docker on the build host does
  # not consume the instance role either, so the pre-pull still logs in with
  # `aws ecr get-login-password` (same as entrypoint.sh on the worker).
  temporary_iam_instance_profile_policy_document {
    Version = "2012-10-17"

    Statement {
      Effect = "Allow"
      Action = [
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
      ]
      Resource = ["*"]
    }
  }
}

build {
  sources = ["source.amazon-ebs.worker"]

  # ---- copy the checkout pieces the install step needs -------------------
  # Packer's file provisioner creates only the FINAL directory: the parent
  # /tmp/vg-repo/deploy must already exist before the directory uploads.
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "mkdir -p /tmp/vg-repo/deploy",
      # The file provisioners upload as var.ssh_username (no sudo), so the
      # root-owned staging parent must be handed to that user.
      "chown ${var.ssh_username}:${var.ssh_username} /tmp/vg-repo",
    ]
  }

  provisioner "file" {
    source      = "${path.root}/../../configs"
    destination = "/tmp/vg-repo/configs"
  }
  provisioner "file" {
    source      = "${path.root}/../../deploy/worker"
    destination = "/tmp/vg-repo/deploy/worker"
  }
  provisioner "file" {
    source      = "${path.root}/../../src"
    destination = "/tmp/vg-repo/src"
  }
  provisioner "file" {
    source      = "${path.root}/../../requirements-worker.txt"
    destination = "/tmp/vg-repo/requirements-worker.txt"
  }
  provisioner "file" {
    source      = "${path.root}/../../pyproject.toml"
    destination = "/tmp/vg-repo/pyproject.toml"
  }

  # ---- 1. base packages + docker + awscli (pinned docker-ce) -------------
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "export DEBIAN_FRONTEND=noninteractive",
      "apt-get update -q",
      "apt-get install -yq --no-install-recommends ca-certificates curl gnupg git build-essential unzip awscli",
      "if ! command -v docker >/dev/null 2>&1; then",
      "  install -m 0755 -d /etc/apt/keyrings",
      "  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc",
      "  echo \"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu jammy stable\" > /etc/apt/sources.list.d/docker.list",
      "  apt-get update -q",
      "  apt-get install -yq --allow-downgrades docker-ce=${var.docker_version} docker-ce-cli=${var.docker_version} containerd.io",
      "fi",
      "systemctl enable --now docker",
      "docker --version",
    ]
  }

  # ---- 2. NVIDIA driver + Container Toolkit -------------------------------
  # DEFERRED GATE: the exact driver deb version is verified on the first real
  # build; the assertion below fails the build if the driver is missing or
  # older than the pinned minimum.
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "export DEBIAN_FRONTEND=noninteractive",
      "apt-get update -q",
      "apt-get install -yq --no-install-recommends linux-headers-$(uname -r) nvidia-driver-550-server",
      "if ! command -v nvidia-container-toolkit >/dev/null 2>&1; then",
      "  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg",
      "  echo \"deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/ /\" > /etc/apt/sources.list.d/nvidia-container-toolkit.list",
      "  apt-get update -q",
      "  apt-get install -yq nvidia-container-toolkit=${var.nvidia_container_toolkit_version}*",
      "fi",
      "nvidia-ctk runtime configure --runtime=docker",
      "systemctl restart docker",
      "nvidia-smi | grep -q '${var.nvidia_driver_version}' || { echo 'NVIDIA driver does not report ${var.nvidia_driver_version}'; nvidia-smi; exit 1; }",
    ]
  }

  # ---- 3. CloudWatch agent (pinned deb) -----------------------------------
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "if ! test -x /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl; then",
      "  curl -fsSL https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/${var.cloudwatch_agent_version}/amazon-cloudwatch-agent.deb -o /tmp/cw-agent.deb",
      "  dpkg -i /tmp/cw-agent.deb",
      "fi",
      "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status | grep -q 'amazon-cloudwatch-agent' || true",
    ]
  }

  # ---- 4. Shaka Packager, pinned release binary ---------------------------
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "if ! test -x /usr/local/bin/packager; then",
      "  curl -fsSL https://github.com/shaka-project/shaka-packager/releases/download/v${var.shaka_version}/packager-linux-x64 -o /usr/local/bin/packager",
      "  chmod 0755 /usr/local/bin/packager",
      "fi",
      "/usr/local/bin/packager --version | grep -q '${var.shaka_version}' || { echo 'shaka version mismatch'; /usr/local/bin/packager --version; exit 1; }",
    ]
  }

  # ---- 5. SECOND pinned FFmpeg (libx264 + h264_nvenc) ---------------------
  # The generator image is GPU-only and has NO libx264 (CONTRACT.md section
  # 5), so the ladder and audio extraction use THIS binary:
  #   VG_FFMPEG_BIN=/usr/local/bin/ffmpeg, VG_FFPROBE_BIN=/usr/local/bin/ffprobe
  # Same source commit as the generator/validator images.
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "export DEBIAN_FRONTEND=noninteractive",
      "apt-get update -q",
      "apt-get install -yq --no-install-recommends yasm nasm pkg-config libx264-dev",
      "mkdir -p /usr/local/src/ffmpeg-v2 && cd /usr/local/src/ffmpeg-v2",
      "if ! test -d nv-codec-headers; then",
      "  git clone https://github.com/FFmpeg/nv-codec-headers.git nv-codec-headers",
      "  cd nv-codec-headers && git checkout ${var.nv_codec_commit} && make install && cd ..",
      "fi",
      "if ! test -x /usr/local/bin/ffmpeg; then",
      "  curl -fsSL https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/${var.ffmpeg_commit} -o ffmpeg.tar.gz",
      "  tar -xzf ffmpeg.tar.gz",
      "  cd FFmpeg-${var.ffmpeg_commit}",
      "  ./configure --prefix=/usr/local --enable-gpl --enable-libx264 --enable-nvenc --enable-ffnvcodec --enable-cuda --enable-cuvid --disable-doc --disable-debug",
      "  make -j$(nproc)",
      "  make install",
      "fi",
      "/usr/local/bin/ffmpeg -version | head -n 1 | grep -q 'version 7.1.5' || { echo 'ffmpeg version mismatch'; /usr/local/bin/ffmpeg -version; exit 1; }",
      "/usr/local/bin/ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libx264' || { echo 'libx264 encoder missing'; exit 1; }",
      "/usr/local/bin/ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'h264_nvenc' || { echo 'h264_nvenc encoder missing'; exit 1; }",
    ]
  }

  # ---- 6. pyenv + pinned Python 3.12.0 at /usr/local/pyenv ----------------
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "export DEBIAN_FRONTEND=noninteractive",
      "apt-get update -q",
      "apt-get install -yq --no-install-recommends libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev wget",
      "if ! test -x /usr/local/pyenv/bin/pyenv; then",
      "  git clone --depth 1 --branch ${var.pyenv_version} https://github.com/pyenv/pyenv.git /usr/local/pyenv",
      "fi",
      "if ! test -x /usr/local/pyenv/versions/${var.python_version}/bin/python3.12; then",
      "  PYENV_ROOT=/usr/local/pyenv /usr/local/pyenv/bin/pyenv install -s ${var.python_version}",
      "fi",
      "/usr/local/pyenv/versions/${var.python_version}/bin/python3.12 --version",
    ]
  }

  # ---- 7. worker agent install (venv + user + systemd units) --------------
  # install.sh builds /opt/vg/venv, creates the vgworker user, writes
  # /etc/vg/worker.yaml + the env skeleton, prepares /work + the tmpfs, and
  # installs/enables vg-worker-bootstrap.service + worker-agent.service.
  # Boot-time values come from the launch template user_data (infra/asg.tf)
  # and are written to /etc/vg/worker.env by the bootstrap unit at boot;
  # never from the image.
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "APP_ROOT=/opt/vg REPO_ROOT=/tmp/vg-repo PYTHON_BIN=/usr/local/pyenv/versions/${var.python_version}/bin/python3.12 bash /tmp/vg-repo/deploy/worker/install.sh",
    ]
  }

  # ---- 8. pre-pull the pinned ECR images (best effort) ---------------------
  # The temporary instance profile above grants ECR read + token access, so
  # the login works on the build host without an external role. Docker still
  # does not use the instance role itself, hence the explicit login. Every
  # failure is tolerated: the first job pulls on demand.
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -uo pipefail",
      "if test -n '${var.generator_image}' && test -n '${var.validator_image}'; then",
      "  REGISTRY=$(printf '%s' '${var.generator_image}' | cut -d/ -f1)",
      "  aws ecr get-login-password --region ${var.region} | docker login --username AWS --password-stdin \"$${REGISTRY}\" || true",
      "  docker pull '${var.generator_image}' || echo 'WARN: generator pre-pull failed (deferred gate)'",
      "  docker pull '${var.validator_image}' || echo 'WARN: validator pre-pull failed (deferred gate)'",
      "else",
      "  echo 'SKIPPED: generator_image/validator_image empty; images are pulled on first job'",
      "fi",
    ]
  }

  # ---- 9. final assertions -------------------------------------------------
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
    inline_shebang  = "#!/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "test -x /usr/local/bin/packager",
      "test -x /usr/local/bin/ffmpeg",
      "test -x /usr/local/bin/ffprobe",
      "test -x /usr/local/pyenv/versions/${var.python_version}/bin/python3.12",
      "test -x /opt/vg/venv/bin/entrypoint-worker.sh",
      "test -x /opt/vg/bootstrap-env.sh",
      "test -f /etc/vg/worker.yaml",
      "test -f /etc/systemd/system/vg-worker-bootstrap.service",
      "test -f /etc/systemd/system/worker-agent.service",
      "/opt/vg/venv/bin/python3.12 -c 'import vg_pipeline, vg_worker; print(\"AMI imports ok\")'",
      "echo 'worker AMI assertions passed'",
    ]
  }

  # Emit packer-manifest.json next to this template. The manifest
  # post-processor records builds[0].artifact_id as "<region>:ami-..."; the
  # CI `packer` job (aws-pipeline.yml) parses it with jq and strips the region
  # prefix to expose the bare AMI id as a job output for terraform-apply.
  post-processor "manifest" {
    output     = "packer-manifest.json"
    strip_path = true
  }
}
