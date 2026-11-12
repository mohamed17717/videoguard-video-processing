"""vg_pipeline: reusable media-processing library for the video-guard worker.

Modules:

* :mod:`vg_pipeline.ab_pattern` — the A/B variant choice per session.
* :mod:`vg_pipeline.contracts` — pydantic v2 models and the pipeline config.
* :mod:`vg_pipeline.errors` — :class:`PipelineError` with stable codes.
* :mod:`vg_pipeline.logging` — JSON logging on stderr and secret redaction.
* :mod:`vg_pipeline.probe` — ffprobe parsing and input-contract validation.
* :mod:`vg_pipeline.clamav` — antivirus scan of the upload.
* :mod:`vg_pipeline.ladder` — NVENC/CPU encode commands, audio, thumbnails.
* :mod:`vg_pipeline.cenc` — Axinom key request (Go port) and PSSH boxes.
* :mod:`vg_pipeline.mp4` — init canonicalisation and tfdt reading.
* :mod:`vg_pipeline.shaka` — packager command and aligned-timeline gate.
* :mod:`vg_pipeline.artifacts` — canonical HLS/DASH manifests, manifest.json.
"""

from __future__ import annotations

from .ab_pattern import pattern, pattern_for_count
from .artifacts import build_canonical_manifests, build_dash_mpd, build_manifest_json, manifest_json_bytes
from .cenc import build_combined_pssh, build_pssh_box, request_cenc_keys
from .clamav import build_scan_command, scan
from .contracts import (
    AlignedTimeline,
    AxinomSecrets,
    CencKeys,
    JobMessage,
    JobSidecar,
    ManifestSet,
    PipelineConfig,
    ProbeResult,
    Rendition,
    ScanResult,
    StreamSpec,
    WebhookEvent,
    load_pipeline_config,
)
from .errors import ERROR_CODES, PipelineError
from .ladder import build_ffmpeg_command, cpu_crf, extract_audio, extract_thumbnails
from .logging import configure_logging, get_logger, redact
from .mp4 import canonicalize_init, read_tfdt
from .probe import expected_qualities, probe, validate_input
from .shaka import build_packager_args, check_aligned

__version__ = "1.0.0"

__all__ = [
    "AlignedTimeline",
    "AxinomSecrets",
    "CencKeys",
    "ERROR_CODES",
    "JobMessage",
    "JobSidecar",
    "ManifestSet",
    "PipelineConfig",
    "PipelineError",
    "ProbeResult",
    "Rendition",
    "ScanResult",
    "StreamSpec",
    "WebhookEvent",
    "build_canonical_manifests",
    "build_combined_pssh",
    "build_dash_mpd",
    "build_ffmpeg_command",
    "build_manifest_json",
    "build_packager_args",
    "build_pssh_box",
    "build_scan_command",
    "canonicalize_init",
    "check_aligned",
    "configure_logging",
    "cpu_crf",
    "expected_qualities",
    "extract_audio",
    "extract_thumbnails",
    "get_logger",
    "load_pipeline_config",
    "manifest_json_bytes",
    "pattern",
    "pattern_for_count",
    "probe",
    "read_tfdt",
    "redact",
    "request_cenc_keys",
    "scan",
    "validate_input",
]
