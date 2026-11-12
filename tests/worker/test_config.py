"""Worker config loading, env merging, and readiness checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vg_worker.config import load_worker_config

from conftest import worker_config_dict


def write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_load_from_yaml_with_defaults(tmp_path: Path) -> None:
    cfg = load_worker_config(write_yaml(tmp_path / "worker.yaml", worker_config_dict(tmp_path)), env={})
    assert cfg.s3_bucket == "vg-media-dev"
    assert cfg.aws_region == "us-east-1"
    assert cfg.deadlines.job_sec == 3600
    assert cfg.deadlines.heartbeat_sec == 300
    assert cfg.queue.visibility_timeout_sec == 900
    assert cfg.prefixes.renditions == "renditions/"
    assert cfg.is_ready() is True
    assert cfg.missing_settings() == []


def test_env_overrides_yaml(tmp_path: Path) -> None:
    config_path = write_yaml(tmp_path / "worker.yaml", worker_config_dict(tmp_path))
    env = {
        "VG_S3_BUCKET": "vg-media-prod",
        "VG_SQS_QUEUE_URL": "https://sqs.prod/jobs",
        "VG_SQS_DLQ_URL": "https://sqs.prod/dlq",
        "VG_WEBHOOK_URL": "https://railway-prod/webhook",
        "VG_WEBHOOK_SECRET_ARN": "arn:prod:wh",
        "VG_AXINOM_SECRET_ARN": "arn:prod:axinom",
        "VG_WATERMARK_SECRET_ARN": "arn:prod:wm",
        "VG_ASG_NAME": "vg-workers-prod",
        "VG_JOB_DEADLINE_SEC": "7200",  # numeric strings parse into ints
        "VG_CONFIG_PATH": "/etc/vg/pipeline-prod.yaml",
        "VG_WORK_DIR": "/data/work",
        "VG_GENERATOR_IMAGE": "ecr-prod/generator:pinned",
        "VG_VALIDATOR_IMAGE": "ecr-prod/validator:pinned",
        "VG_PACKAGER_BIN": "/opt/packager",
        "VG_FFPROBE_BIN": "/opt/ffprobe",
        "VG_FFMPEG_BIN": "/opt/ffmpeg",
        "VG_LOG_LEVEL": "DEBUG",
    }
    cfg = load_worker_config(config_path, env)
    assert cfg.s3_bucket == "vg-media-prod"
    assert cfg.queue.jobs_url == "https://sqs.prod/jobs"
    assert cfg.queue.dlq_url == "https://sqs.prod/dlq"
    assert cfg.webhook.url == "https://railway-prod/webhook"
    assert cfg.secrets.webhook_secret_arn == "arn:prod:wh"
    assert cfg.secrets.axinom_secret_arn == "arn:prod:axinom"
    assert cfg.secrets.watermark_secret_arn == "arn:prod:wm"
    assert cfg.asg_name == "vg-workers-prod"
    assert cfg.deadlines.job_sec == 7200
    assert cfg.pipeline_config_path == "/etc/vg/pipeline-prod.yaml"
    assert cfg.work_dir == "/data/work"
    assert cfg.images.generator == "ecr-prod/generator:pinned"
    assert cfg.images.validator == "ecr-prod/validator:pinned"
    assert cfg.bins.packager == "/opt/packager"
    assert cfg.bins.ffprobe == "/opt/ffprobe"
    assert cfg.bins.ffmpeg == "/opt/ffmpeg"
    assert cfg.log_level == "DEBUG"


def test_empty_env_values_are_ignored(tmp_path: Path) -> None:
    config_path = write_yaml(tmp_path / "worker.yaml", worker_config_dict(tmp_path))
    cfg = load_worker_config(config_path, {"VG_S3_BUCKET": "", "VG_NOTHING": "irrelevant"})
    assert cfg.s3_bucket == "vg-media-dev"


def test_unknown_env_vars_do_not_conflict(tmp_path: Path) -> None:
    config_path = write_yaml(tmp_path / "worker.yaml", worker_config_dict(tmp_path))
    cfg = load_worker_config(config_path, {"VG_UNKNOWN": "zzz"})
    assert cfg.s3_bucket == "vg-media-dev"


def test_missing_reasonable_file_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_worker_config(tmp_path / "nope.yaml", {})


def test_non_mapping_yaml_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_worker_config(path, {})


def test_bad_schema_version_fails(tmp_path: Path) -> None:
    data = worker_config_dict(tmp_path)
    data["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        load_worker_config(write_yaml(tmp_path / "bad.yaml", data), {})


def test_job_deadline_must_be_gt_heartbeat(tmp_path: Path) -> None:
    data = worker_config_dict(tmp_path)
    data["deadlines"]["job_sec"] = 300
    data["deadlines"]["heartbeat_sec"] = 300
    with pytest.raises(ValueError, match="heartbeat_sec"):
        load_worker_config(write_yaml(tmp_path / "bad.yaml", data), {})


def test_not_ready_lists_missing() -> None:
    from vg_worker.config import WorkerConfig

    cfg = WorkerConfig()  # all defaults/empties
    missing = cfg.missing_settings()
    assert "s3_bucket" in missing
    assert "queue.jobs_url" in missing
    assert "images.generator" in missing
    assert cfg.is_ready() is False


def test_deadline_config_limits(tmp_path: Path) -> None:
    data = worker_config_dict(tmp_path)
    data["deadlines"]["kill_grace_sec"] = 10  # must be 30..60
    with pytest.raises(Exception):  # noqa: B017,PT011 — pydantic ValidationError
        load_worker_config(write_yaml(tmp_path / "bad.yaml", data), {})
