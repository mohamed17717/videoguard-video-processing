"""Static checks for the deploy artifacts that boot and provision the worker.

These files are shell/systemd/Dockerfile, not HCL, so they live in their own
module: the fixes they cover are the boot-time user-data loader, the systemd
ordering that makes a loader failure fatal, the ECR login in entrypoint.sh,
and the validator image's FFmpeg build prerequisites. No AWS, GPU or Docker
build runs here (CONTRACT.md section 11 deferred gates).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = REPO_ROOT / "deploy" / "worker"
VALIDATOR_DOCKERFILE = REPO_ROOT / "deploy" / "validator" / "Dockerfile"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    ["bootstrap-env.sh", "install.sh", "entrypoint.sh"],
)
def test_shell_scripts_parse(name: str) -> None:
    subprocess.run(["bash", "-n", str(WORKER_DIR / name)], check=True)


def test_bootstrap_loader_uses_imdsv2_and_writes_atomically() -> None:
    """Fix #2: the launch template's user_data is base64(json); this loader
    is the only reader. It must use IMDSv2 (tokens required), validate the
    VG_* namespace, and replace /etc/vg/worker.env atomically."""
    text = _read(WORKER_DIR / "bootstrap-env.sh")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "/latest/api/token" in text
    assert "X-aws-ec2-metadata-token-ttl-seconds" in text
    assert "/latest/user-data" in text
    assert "X-aws-ec2-metadata-token:" in text
    assert "VG_[A-Z0-9_]+" in text
    assert "base64.b64decode" in text
    assert "os.replace" in text
    assert "0o644" in text
    assert "ENV_FILE" in text and "/etc/vg/worker.env" in text
    # A missing/invalid user-data must be a hard failure, never an empty file.
    assert "die " in text
    assert "user data is empty" in text


def test_bootstrap_unit_runs_oneshot_as_root_before_the_agent() -> None:
    text = _read(WORKER_DIR / "vg-worker-bootstrap.service")
    assert "Type=oneshot" in text
    assert "User=root" in text
    assert "After=network-online.target" in text
    assert "Before=worker-agent.service" in text
    assert "ExecStart=/opt/vg/bootstrap-env.sh" in text
    assert "RemainAfterExit=yes" in text


def test_agent_unit_requires_the_bootstrap_loader() -> None:
    text = _read(WORKER_DIR / "worker-agent.service")
    assert "Requires=vg-worker-bootstrap.service" in text
    assert re.search(
        r"^After=.*\bvg-worker-bootstrap\.service\b", text, re.MULTILINE
    ), "worker-agent.service must start after the bootstrap loader"
    assert "EnvironmentFile=/etc/vg/worker.env" in text
    # Fix #26a: docker login must write inside the vgworker-owned state dir.
    assert "Environment=DOCKER_CONFIG=/var/lib/vgworker/.docker" in text
    assert "ReadWritePaths=/work /var/lib/vgworker /run/vg-secrets" in text


def test_install_script_installs_bootstrap_and_docker_config_dir() -> None:
    text = _read(WORKER_DIR / "install.sh")
    assert '"$REPO_ROOT/deploy/worker/bootstrap-env.sh"' in text
    assert '"$APP_ROOT/bootstrap-env.sh"' in text
    assert '"$REPO_ROOT/deploy/worker/vg-worker-bootstrap.service"' in text
    assert "systemctl enable vg-worker-bootstrap.service" in text
    assert "/var/lib/vgworker/.docker" in text
    # The skeleton stays as a fallback (the loader overwrites it at boot).
    assert "VG_AWS_REGION=" in text
    assert "VG_S3_BUCKET=" in text


def test_entrypoint_authenticates_to_ecr_before_exec() -> None:
    text = _read(WORKER_DIR / "entrypoint.sh")
    assert "${VG_GENERATOR_IMAGE:?" in text
    assert "${VG_AWS_REGION:?" in text
    assert "aws ecr get-login-password" in text
    assert "docker login --username AWS --password-stdin" in text
    assert "${VG_GENERATOR_IMAGE%%/*}" in text
    assert text.index("aws ecr get-login-password") < text.index(
        "exec /opt/vg/venv/bin/python3.12"
    ), "ECR login must happen before the agent exec"


def test_validator_dockerfile_has_an_assembler_and_the_license() -> None:
    """Fixes #8/#9: FFmpeg's x86asm is not covered by --disable-autodetect,
    and `make install` does not copy COPYING.LGPLv2.1 into the prefix."""
    text = _read(VALIDATOR_DOCKERFILE)
    builder = text.split("AS runtime", 1)[0]
    assert re.search(r"^\s+nasm \\$", builder, re.MULTILINE), (
        "the ffprobe builder must install nasm for FFmpeg's x86 assembler"
    )
    assert (
        'cp "/sources/FFmpeg-${FFMPEG_COMMIT}/COPYING.LGPLv2.1" '
        "/opt/ffmpeg/COPYING.LGPLv2.1"
    ) in builder
    assert "COPYING.LGPLv2.1" in text.split("COPY --from=ffprobe-builder", 1)[1]
