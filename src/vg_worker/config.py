"""Worker configuration: ``configs/aws.worker.yaml`` plus ``VG_*`` env vars.

The worker is a host systemd service, so its runtime settings come from one
YAML file on disk plus environment variables (CONTRACT.md section 5). YAML is
the baseline; any ``VG_*`` variable that is set to a non-empty value wins.

Values here are deploy-time settings only. The media-processing profile lives
in ``configs/pipeline.yaml`` (``VG_CONFIG_PATH``) and is loaded separately
with :func:`vg_pipeline.load_pipeline_config`.

No secret values ever live in this file or the environment: the environment
carries secret *ARNs*, and the secret contents are fetched from Secrets
Manager at run time (see :mod:`vg_worker.secrets`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import BaseModel, Field

# Which environment variable overrides which YAML key. The tuple is the path
# inside the YAML document, e.g. ("queue", "jobs_url") -> queue.jobs_url.
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "VG_AWS_REGION": ("aws_region",),
    "VG_S3_BUCKET": ("s3_bucket",),
    "VG_SQS_QUEUE_URL": ("queue", "jobs_url"),
    "VG_SQS_DLQ_URL": ("queue", "dlq_url"),
    "VG_WEBHOOK_URL": ("webhook", "url"),
    "VG_WEBHOOK_SECRET_ARN": ("secrets", "webhook_secret_arn"),
    "VG_AXINOM_SECRET_ARN": ("secrets", "axinom_secret_arn"),
    "VG_WATERMARK_SECRET_ARN": ("secrets", "watermark_secret_arn"),
    "VG_ASG_NAME": ("asg_name",),
    "VG_JOB_DEADLINE_SEC": ("deadlines", "job_sec"),
    "VG_CONFIG_PATH": ("pipeline_config_path",),
    "VG_WORK_DIR": ("work_dir",),
    "VG_GENERATOR_IMAGE": ("images", "generator"),
    "VG_VALIDATOR_IMAGE": ("images", "validator"),
    "VG_PACKAGER_BIN": ("bins", "packager"),
    "VG_FFPROBE_BIN": ("bins", "ffprobe"),
    "VG_FFMPEG_BIN": ("bins", "ffmpeg"),
    "VG_LOG_LEVEL": ("log_level",),
}


class QueueConfig(BaseModel):
    """Jobs queue plus the DLQ (CONTRACT.md section 2)."""

    jobs_url: str = ""
    dlq_url: str = ""
    visibility_timeout_sec: int = Field(default=900, gt=0)
    receive_wait_sec: int = Field(default=20, ge=0, le=20)
    # SQS redrive limit. The worker also uses it to decide "final attempt"
    # for the ``failed`` webhook (CONTRACT.md section 3).
    max_receive_count: int = Field(default=3, gt=0)


class WebhookConfig(BaseModel):
    """Railway webhook endpoint settings (CONTRACT.md section 3)."""

    url: str = ""
    request_timeout_sec: float = Field(default=5.0, gt=0)
    backoff: list[float] = Field(default_factory=lambda: [1.0, 2.0, 4.0])


class DeadlineConfig(BaseModel):
    """Job deadline, heartbeat cadence and lock lifetime (seconds)."""

    job_sec: int = Field(default=3600, gt=0)
    heartbeat_sec: int = Field(default=300, gt=0)
    lock_ttl_sec: int = Field(default=1200, gt=0)
    # SIGTERM is sent at the deadline; SIGKILL follows 30-60 s later.
    kill_grace_sec: int = Field(default=45, ge=30, le=60)


class ImageConfig(BaseModel):
    """Container images pinned at build time (ECR tags, not mutable)."""

    generator: str = ""
    validator: str = ""


class BinConfig(BaseModel):
    """Host binary paths (CONTRACT.md section 5)."""

    docker: str = "docker"
    packager: str = "/usr/local/bin/packager"
    ffprobe: str = "/usr/local/bin/ffprobe"
    ffmpeg: str = "/usr/local/bin/ffmpeg"


class SecretArnConfig(BaseModel):
    """Secrets Manager ARNs; contents are fetched at run time."""

    webhook_secret_arn: str = ""
    axinom_secret_arn: str = ""
    watermark_secret_arn: str = ""


class PrefixConfig(BaseModel):
    """S3 key prefixes (CONTRACT.md section 1). Trailing slash included."""

    incoming: str = "incoming/"
    jobs: str = "jobs/"
    masters: str = "masters/"
    reports: str = "reports/"
    renditions: str = "renditions/"


class DiskConfig(BaseModel):
    """Free-space gate before work starts (CONTRACT.md section 2)."""

    headroom_multiplier: float = Field(default=2.5, gt=0)
    headroom_gib: int = Field(default=20, gt=0)


class WorkerConfig(BaseModel):
    """Everything the worker process needs from its configuration files.

    Typed model: validated when loaded, so a broken config fails at start-up
    with a plain Python error, not a media-pipeline job failure.
    """

    schema_version: int = 1
    aws_region: str = "us-east-1"
    s3_bucket: str = ""
    queue: QueueConfig = Field(default_factory=QueueConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    deadlines: DeadlineConfig = Field(default_factory=DeadlineConfig)
    work_dir: str = "/work"
    log_level: str = "INFO"
    backend: str = "cuda"
    pipeline_config_path: str = ""
    # Tmpfs directory where the watermark secret is staged for the generator
    # container (CONTRACT.md section 5); the AMI mounts it at boot.
    secret_file_dir: str = "/run/vg-secrets"
    images: ImageConfig = Field(default_factory=ImageConfig)
    bins: BinConfig = Field(default_factory=BinConfig)
    secrets: SecretArnConfig = Field(default_factory=SecretArnConfig)
    asg_name: str = ""
    prefixes: PrefixConfig = Field(default_factory=PrefixConfig)
    disk: DiskConfig = Field(default_factory=DiskConfig)

    def missing_settings(self) -> list[str]:
        """Names of required settings that are still empty/placeholders."""
        missing: list[str] = []
        required: dict[str, str] = {
            "s3_bucket": self.s3_bucket,
            "queue.jobs_url": self.queue.jobs_url,
            "queue.dlq_url": self.queue.dlq_url,
            "webhook.url": self.webhook.url,
            "secrets.webhook_secret_arn": self.secrets.webhook_secret_arn,
            "secrets.axinom_secret_arn": self.secrets.axinom_secret_arn,
            "secrets.watermark_secret_arn": self.secrets.watermark_secret_arn,
            "asg_name": self.asg_name,
            "images.generator": self.images.generator,
            "images.validator": self.images.validator,
            "pipeline_config_path": self.pipeline_config_path,
        }
        for name, value in required.items():
            if not value:
                missing.append(name)
        return missing

    def is_ready(self) -> bool:
        """True when the config has every required value set."""
        return not self.missing_settings()


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: str) -> None:
    """Set ``data[A][B]... [last] = value``, creating section dicts as needed."""
    node: Any = data
    for part in path[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[path[-1]] = value


def apply_env(data: dict[str, Any], env: Mapping[str, str]) -> None:
    """Overlay the non-empty ``VG_*`` variables from ``env`` onto ``data``."""
    for name, path in _ENV_OVERRIDES.items():
        value = env.get(name, "")
        if value.strip() == "":
            continue
        try:
            _set_nested(data, path, value)
        except TypeError as exc:  # a YAML section has a scalar where a dict is needed
            raise ValueError(f"config section for {name} is not a mapping: {exc}") from exc


def load_worker_config(path: str | Any, env: Mapping[str, str]) -> WorkerConfig:
    """Load :class:`WorkerConfig` from ``path`` merged with ``VG_*`` env vars.

    YAML wins only when no environment variable overrides the same key.
    Raises ``FileNotFoundError`` for a missing file, ``ValueError`` for a file
    that is not a mapping, and ``pydantic.ValidationError`` for values with
    the wrong type. These are start-up failures, not job failures.
    """
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"worker config must be a YAML mapping, got {type(data).__name__}")
    if data.get("schema_version", 1) != 1:
        raise ValueError(f"unsupported worker config schema_version: {data.get('schema_version')!r}")
    apply_env(data, env)
    cfg = WorkerConfig.model_validate(data)
    if cfg.deadlines.job_sec <= cfg.deadlines.heartbeat_sec:
        raise ValueError(
            "deadlines.job_sec must be greater than deadlines.heartbeat_sec "
            f"({cfg.deadlines.job_sec} <= {cfg.deadlines.heartbeat_sec})"
        )
    return cfg
