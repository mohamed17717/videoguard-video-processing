#!/usr/bin/env bash
# Entry point for the video-guard worker agent's systemd unit.
# Sources the secret-free environment (VG_* variable names), then execs the
# Python agent. The agent itself terminates the instance when the message is
# done — this script NEVER runs the agent twice.
set -euo pipefail

# SENTINELS: the install script writes these files and the Packer AMI
# provisions the rest; fail loudly if a deploy step was skipped.
test -f /etc/vg/worker.env
test -x /opt/vg/venv/bin/python3.12

# Load VG_* (SQS URLs, ARNs, image refs, bins). No secret VALUES here: only
# reserved ARNs and URLs; the agent fetches secret contents per job.
set -a
. /etc/vg/worker.env
set +a

# ECR authentication. Docker never uses the EC2 instance role, so the CLI
# must log in explicitly before the agent pulls the generator/validator
# images (infra/iam.tf grants ecr:GetAuthorizationToken + image reads).
# VG_GENERATOR_IMAGE is the contract image reference
# "<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>", so its first path
# component is the registry host.
: "${VG_GENERATOR_IMAGE:?VG_GENERATOR_IMAGE is required for ECR authentication}"
: "${VG_AWS_REGION:?VG_AWS_REGION is required for ECR authentication}"

registry="${VG_GENERATOR_IMAGE%%/*}"
case "$registry" in
    *.dkr.ecr.*.amazonaws.com) ;;
    *)
        echo "VG_GENERATOR_IMAGE does not point at an ECR registry: $VG_GENERATOR_IMAGE" >&2
        exit 1
        ;;
esac

# Fail loudly: without this login every `docker pull` returns 403 and no job
# can run. DOCKER_CONFIG (worker-agent.service) keeps the credential file in
# the vgworker-owned /var/lib/vgworker/.docker directory.
aws ecr get-login-password --region "$VG_AWS_REGION" \
    | docker login --username AWS --password-stdin "$registry"

# The agent needs docker; vgworker is in the `docker` group (install.sh).
cd /opt/vg

# Use the pyenv-built Python 3.12 from the venv (CONTRACT.md section 10).
exec /opt/vg/venv/bin/python3.12 -m vg_worker.main --config /etc/vg/worker.yaml
