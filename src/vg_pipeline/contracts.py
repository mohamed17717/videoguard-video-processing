"""Pydantic v2 data contracts shared by the pipeline library and the worker.

Every message, manifest and config that crosses a process or network boundary
is one of these models. Field names and defaults follow CONTRACT.md sections
1, 3 and 4; do not rename fields here without changing the contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class JobSidecar(BaseModel):
    """The sidecar object the backend writes to ``jobs/<job_id>/job.json``.

    Written before the presigned upload URL is returned, so the worker can
    learn ``video_public_id`` (the raw S3 event does not carry it).
    """

    schema_version: int = 1
    job_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    run_epoch: int = Field(ge=1, description="monotonic uint64, ordered by the backend")
    video_public_id: str = Field(min_length=1)
    tenant_slug: str = Field(min_length=1)
    content_id: str = Field(
        min_length=1,
        description="must equal video_public_id; the Axinom content id",
    )
    content_type: str = "video/mp4"
    size_bytes: int = Field(ge=0)
    retention: Literal["archive", "ttl30"] = "archive"
    trace_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1, description="RFC3339 timestamp")
    reason: str = "s3"


class JobMessage(JobSidecar):
    """Canonical SQS message: the sidecar schema plus ``reason``.

    A raw S3 event is normalised into this shape by the worker. A requeue
    carries a NEW ``run_id`` and a strictly greater ``run_epoch``.
    """

    reason: Literal["s3", "requeue"] = "s3"


class WebhookEvent(BaseModel):
    """One progress webhook body (CONTRACT.md section 3).

    Ordered by ``(run_epoch, attempt, seq)``, never by the random ``run_id``.
    ``seq`` restarts at 1 on each worker attempt.
    """

    schema_version: int = 1
    job_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    run_epoch: int = Field(ge=1)
    stage: str = Field(min_length=1)
    seq: int = Field(ge=1)
    attempt: int = Field(ge=1)
    timestamp: int = Field(ge=0, description="unix seconds")
    qualifier: str = ""
    retryable: bool = False
    payload: dict = Field(default_factory=dict)


class Rendition(BaseModel):
    """One ladder rung (CONTRACT.md section 4)."""

    name: str = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    target_kbps: int = Field(gt=0)
    maxrate_kbps: int = Field(gt=0)
    bufsize_kbps: int = Field(gt=0)
    nvenc_cq: int = Field(ge=0, le=51)


class PipelineConfig(BaseModel):
    """Root of ``configs/pipeline.yaml`` (CONTRACT.md section 4)."""

    schema_version: int = 1
    profile: str
    segment_duration_sec: int = Field(gt=0)
    gop_seconds: int = Field(gt=0)
    thumbnail_count: int = Field(ge=0)
    drm: dict
    ladder: list[Rendition]
    input_contract: dict
    caps: dict


class ProbeResult(BaseModel):
    """Everything ``ffprobe`` reported that the pipeline needs.

    ``audio_streams`` is filled in by :func:`vg_pipeline.probe.probe`. It is
    optional so hand-built instances keep working; when present,
    :func:`vg_pipeline.probe.validate_input` uses it to enforce
    ``max_audio_streams``.
    """

    codec: str
    width: int
    height: int
    pix_fmt: str
    fps: float
    duration_sec: float
    frame_count: int
    bit_depth: int
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_range: str | None = None
    has_audio: bool
    audio_codec: str | None = None
    streams: int
    container: str
    vfr: bool
    audio_streams: int | None = None


class CencKeys(BaseModel):
    """One CENC key set for packaging.

    ``key_hex`` and ``iv_hex`` are secrets: never log them, never write them
    to a tracked file or a published manifest.
    """

    key_id_hex: str
    key_hex: str
    iv_hex: str
    pssh_hex: str
    skd_uri: str


class AlignedTimeline(BaseModel):
    """Result of the aligned-timeline gate (CONTRACT.md section 6).

    ``segment_counts`` maps quality name to the segment count (equal for the
    a and b trees, otherwise the gate fails). ``init_identical`` and
    ``tfdt_aligned`` are the other two gate conditions.
    """

    segment_counts: dict[str, int]
    init_identical: bool
    tfdt_aligned: bool
    segment_durations: dict[str, list[float]] = Field(default_factory=dict)
    audio_durations: list[float] = Field(default_factory=list)


class ManifestSet(BaseModel):
    """All canonical manifests of one job, ready for upload.

    ``files`` maps relative S3 keys (no leading slash) to the exact bytes to
    PUT, e.g. ``renditions/<job_id>/hls/master.m3u8``.
    """

    files: dict[str, bytes]


class ScanResult(BaseModel):
    """Outcome of one ClamAV scan. ``output`` is ClamAV's own text."""

    clean: bool
    output: str


class StreamSpec(BaseModel):
    """One input stream for one Shaka Packager run (CONTRACT.md section 6).

    A packaging job passes 9 of these: 8 video (variants a/b times the
    produced qualities) and 1 audio.
    """

    input: Path
    stream: Literal["video", "audio"]
    init_segment: Path
    segment_template: Path
    playlist_name: str
    variant: str
    quality: str


class AxinomSecrets(BaseModel):
    """Axinom CENC service credentials (CONTRACT.md section 5).

    ``signing_key_hex`` and ``signing_iv_hex`` are secrets: hold them in
    process memory only.
    """

    signing_key_hex: str
    signing_iv_hex: str
    endpoint: str
    provider_name: str
    scheme: str = "cbcs"


def load_pipeline_config(path: str | Path) -> PipelineConfig:
    """Load and validate ``configs/pipeline.yaml``.

    Raises ``FileNotFoundError`` when the path is missing and
    ``pydantic.ValidationError`` when the YAML does not match the schema.
    Config problems happen at worker start-up, so they are plain Python
    errors, not :class:`vg_pipeline.errors.PipelineError` job failures.
    """
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"pipeline config must be a YAML mapping, got {type(data).__name__}")
    if data.get("schema_version") != 1:
        raise ValueError(f"unsupported pipeline config schema_version: {data.get('schema_version')!r}")
    return PipelineConfig.model_validate(data)
