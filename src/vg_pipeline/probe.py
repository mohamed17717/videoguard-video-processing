"""Input validation: run ffprobe once and enforce the full input contract.

The worker's ``validated`` stage re-checks the browser upload here and fails
closed with ``INPUT_CONTRACT``. Nothing is converted; a non-compliant upload
is rejected (CONTRACT.md section 1, "Option A, no normalisation").
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .contracts import PipelineConfig, ProbeResult, Rendition
from .errors import PipelineError
from .logging import get_logger

log = get_logger("probe")

# ffmpeg probe flag list. One run, JSON output, streams plus format.
_FFPROBE_ARGS = ("-v", "error", "-print_format", "json", "-show_streams", "-show_format")

# HDR markers that must never appear even when the allowed lists would catch
# them anyway. Named explicitly so the failure message is clear.
_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
_HDR_PRIMARIES = frozenset({"bt2020", "smpte432"})

# Tolerance when comparing r_frame_rate against avg_frame_rate (frames/s).
_FPS_EPS = 1e-6


def _parse_fraction(text: str | None) -> float | None:
    """Parse an ffprobe rate such as ``30000/1001`` or ``30/1``.

    Returns ``None`` for missing or ``0/0`` (unknown) values. NOTE: this does
    NOT repeat the old ffprobe.go ``max(parseFPS, 30)`` bug.
    """
    if not text:
        return None
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    if value <= 0:
        return None
    return float(value)


def _bit_depth(stream: dict[str, Any], pix_fmt: str) -> int:
    """Best-effort bit depth: ffprobe field first, then the pixel format name."""
    raw = stream.get("bits_per_raw_sample")
    if raw is not None:
        try:
            depth = int(raw)
        except (TypeError, ValueError):
            depth = 0
        if depth > 0:
            return depth
    if "10" in pix_fmt:
        return 10
    if "12" in pix_fmt:
        return 12
    if "16" in pix_fmt:
        return 16
    return 8


def _parse_probe_json(stdout: str) -> ProbeResult:
    """Build a :class:`ProbeResult` from ffprobe JSON text."""
    import json

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError("INPUT_CONTRACT", f"ffprobe output is not valid JSON: {exc}") from exc
    streams = data.get("streams") or []
    if not isinstance(streams, list) or not streams:
        raise PipelineError("INPUT_CONTRACT", "ffprobe reported no streams")
    fmt = data.get("format") or {}

    video: dict[str, Any] | None = None
    has_audio = False
    audio_codec: str | None = None
    audio_streams = 0
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        if kind == "video" and video is None:
            video = stream
        elif kind == "audio":
            audio_streams += 1
            has_audio = True
            if audio_codec is None:
                audio_codec = stream.get("codec_name")
    if video is None:
        raise PipelineError("INPUT_CONTRACT", "ffprobe reported no video stream")

    fps = _parse_fraction(video.get("r_frame_rate"))
    avg_fps = _parse_fraction(video.get("avg_frame_rate"))
    if fps is None:
        raise PipelineError("INPUT_CONTRACT", f"ffprobe reported no usable frame rate: {video.get('r_frame_rate')!r}")

    duration = _parse_float(fmt.get("duration"))
    if duration is None:
        duration = _parse_float(video.get("duration")) or 0.0

    frame_count = _parse_int(video.get("nb_frames"))
    if frame_count is None:
        frame_count = int(round(duration * fps))

    pix_fmt = video.get("pix_fmt") or ""
    container = fmt.get("format_name") or ""

    # CFR detection: a constant frame rate reports the same r_frame_rate and
    # avg_frame_rate. An unknown avg_frame_rate proves nothing either way.
    vfr = avg_fps is not None and abs(avg_fps - fps) > _FPS_EPS

    return ProbeResult(
        codec=video.get("codec_name") or "",
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        pix_fmt=pix_fmt,
        fps=fps,
        duration_sec=duration,
        frame_count=frame_count,
        bit_depth=_bit_depth(video, pix_fmt),
        color_transfer=video.get("color_transfer"),
        color_primaries=video.get("color_primaries"),
        color_range=video.get("color_range"),
        has_audio=has_audio,
        audio_codec=audio_codec,
        streams=len(streams),
        container=container,
        vfr=vfr,
        audio_streams=audio_streams,
    )


def _parse_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe(
    path: Path,
    cfg: PipelineConfig,
    ffprobe_bin: str,
    *,
    run: Any = subprocess.run,
) -> ProbeResult:
    """Run ``ffprobe`` once on ``path`` and return the parsed result.

    ``cfg`` is accepted for API symmetry; probing never enforces caps by
    itself — :func:`validate_input` does. ``run`` is injectable for tests.
    Raises :class:`PipelineError` with code ``INPUT_CONTRACT`` when ffprobe
    fails or reports nothing usable.
    """
    cmd = [ffprobe_bin, *_FFPROBE_ARGS, str(path)]
    log.debug("ffprobe start", extra={"path": str(path)})
    try:
        completed = run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise PipelineError("INPUT_CONTRACT", f"cannot run ffprobe: {exc}") from exc
    stderr = getattr(completed, "stderr", "") or ""
    if getattr(completed, "returncode", 0) != 0:
        raise PipelineError(
            "INPUT_CONTRACT",
            f"ffprobe failed with exit {completed.returncode}: {stderr.strip()[-500:]}",
        )
    stdout = getattr(completed, "stdout", "") or ""
    result = _parse_probe_json(stdout)
    log.debug(
        "ffprobe done",
        extra={"path": str(path), "width": result.width, "height": result.height, "fps": result.fps},
    )
    return result


def _container_allowed(container: str, allowed: list[str]) -> bool:
    """ffprobe lists candidate demuxers, e.g. ``mov,mp4,m4a,3gp,3g2,mj2``.

    The container is allowed when at least one listed demuxer is allowed.
    ``matroska,webm`` therefore fails while the normal MP4 family passes.
    """
    parts = [part.strip().lower() for part in container.split(",")]
    return any(part in allowed for part in parts)


def validate_input(result: ProbeResult, cfg: PipelineConfig, size_bytes: int) -> None:
    """Enforce the full input contract and hard caps; raise on ANY violation.

    Every check compares against ``cfg.input_contract`` and ``cfg.caps`` from
    ``configs/pipeline.yaml`` — no hardcoded thresholds here. Colour metadata
    that ffprobe did not report (``None``) is accepted with a warning, which
    mirrors the generator's input contract: untagged colour is assumed to be
    limited-range SDR and should be verified upstream.
    """
    contract = cfg.input_contract
    caps = cfg.caps
    problems: list[str] = []

    if size_bytes > int(caps.get("max_bytes", 0)):
        problems.append(f"size {size_bytes} exceeds max_bytes {caps.get('max_bytes')}")
    if result.duration_sec > float(caps.get("max_duration_sec", 0)):
        problems.append(f"duration {result.duration_sec}s exceeds max_duration_sec {caps.get('max_duration_sec')}")
    if result.width > int(caps.get("max_width", 0)):
        problems.append(f"width {result.width} exceeds max_width {caps.get('max_width')}")
    if result.height > int(caps.get("max_height", 0)):
        problems.append(f"height {result.height} exceeds max_height {caps.get('max_height')}")
    if result.frame_count > int(caps.get("max_frames", 0)):
        problems.append(f"frame count {result.frame_count} exceeds max_frames {caps.get('max_frames')}")
    if result.streams > int(caps.get("max_streams", 0)):
        problems.append(f"stream count {result.streams} exceeds max_streams {caps.get('max_streams')}")

    if contract.get("require_cfr", True) and result.vfr:
        problems.append("video is not constant frame rate (VFR)")
    if contract.get("reject_vfr", True) and result.vfr:
        problems.append("VFR video is rejected")
    if contract.get("reject_hdr", True):
        if result.color_transfer in _HDR_TRANSFERS:
            problems.append(f"HDR transfer function {result.color_transfer} is rejected")
        if result.color_primaries in _HDR_PRIMARIES:
            problems.append(f"HDR primaries {result.color_primaries} is rejected")

    max_bit_depth = int(contract.get("max_bit_depth", 8))
    if result.bit_depth > max_bit_depth:
        problems.append(f"bit depth {result.bit_depth} exceeds max_bit_depth {max_bit_depth}")

    allowed_pix_fmts = [str(v) for v in contract.get("allowed_pix_fmts", [])]
    if allowed_pix_fmts and result.pix_fmt not in allowed_pix_fmts:
        problems.append(f"pixel format {result.pix_fmt!r} is not in {allowed_pix_fmts}")

    allowed_codecs = [str(v) for v in contract.get("allowed_codecs", [])]
    if allowed_codecs and result.codec not in allowed_codecs:
        problems.append(f"video codec {result.codec!r} is not in {allowed_codecs}")

    allowed_containers = [str(v) for v in contract.get("allowed_containers", [])]
    if allowed_containers and not _container_allowed(result.container, allowed_containers):
        problems.append(f"container {result.container!r} is not in {allowed_containers}")

    if contract.get("require_even_dimensions", True):
        if result.width % 2 != 0 or result.height % 2 != 0:
            problems.append(f"dimensions {result.width}x{result.height} are not even")

    # Colour metadata: a reported value must be in the allowed list; a missing
    # value is accepted with a warning (generator rule for untagged sources).
    for field, allowed_key in (
        ("color_transfer", "allowed_color_transfer"),
        ("color_primaries", "allowed_color_primaries"),
        ("color_range", "allowed_color_range"),
    ):
        value = getattr(result, field)
        allowed = [str(v) for v in contract.get(allowed_key, [])]
        if value is None:
            log.warning("input colour metadata missing", extra={"field": field})
        elif allowed and value not in allowed:
            problems.append(f"{field} {value!r} is not in {allowed}")

    if result.has_audio:
        allowed_audio_codecs = [str(v) for v in contract.get("allowed_audio_codecs", [])]
        if allowed_audio_codecs and result.audio_codec not in allowed_audio_codecs:
            problems.append(f"audio codec {result.audio_codec!r} is not in {allowed_audio_codecs}")
    max_audio_streams = int(contract.get("max_audio_streams", 1))
    audio_streams = result.audio_streams if result.audio_streams is not None else (1 if result.has_audio else 0)
    if audio_streams > max_audio_streams:
        problems.append(f"audio streams {audio_streams} exceed max_audio_streams {max_audio_streams}")

    if problems:
        raise PipelineError("INPUT_CONTRACT", "; ".join(problems))
    log.info("input validated", extra={"width": result.width, "height": result.height, "fps": result.fps})


def expected_qualities(result: ProbeResult, cfg: PipelineConfig) -> list[Rendition]:
    """Return the ladder rungs that fit inside the probed source.

    A rung is skipped when its width OR its height exceeds the source, so
    nothing is ever upscaled. ``done.qualities[]`` and every published
    manifest must contain only the returned names (CONTRACT.md section 4).
    """
    produced = [
        rendition
        for rendition in cfg.ladder
        if rendition.width <= result.width and rendition.height <= result.height
    ]
    log.info(
        "qualities selected",
        extra={
            "produced": [r.name for r in produced],
            "skipped": [r.name for r in cfg.ladder if r not in produced],
        },
    )
    return produced
