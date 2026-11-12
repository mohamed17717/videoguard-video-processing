"""Ladder stage: encode every produced quality for BOTH variants locally.

Facts that shape this stage (CONTRACT.md sections 4, 6):

* the ladder is 720/480/360/240p with per-rung NVENC CQ values, and a rung
  whose width or height EXCEEDS the probed source is skipped (never
  upscaled) — if NO rung fits, the job fails closed with ``INPUT_CONTRACT``;
* output codec is H.264: ``h264_nvenc`` (``-rc vbr -cq``, never ``-crf``
  together) from the pinned AMI ffmpeg; if NVENC is unavailable the SAME
  binary's ``libx264`` (``-crf``) is used; if neither works the job fails
  with ``ENCODER_UNAVAILABLE``;
* the audio tree is extracted ONCE (from master A) and shared by both
  variants and all qualities;
* ``thumbnail_count`` thumbnails are cut from the ORIGINAL upload.

This stage deliberately does NOT publish anything to ``renditions/`` (the
published renditions are the ENCRYPTED packager outputs; see the audit note
in :mod:`vg_worker.stages`).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vg_pipeline.contracts import ProbeResult, Rendition
from vg_pipeline.errors import PipelineError
from vg_pipeline.ladder import build_ffmpeg_command, extract_audio, extract_thumbnails
from vg_pipeline.logging import get_logger
from vg_pipeline.probe import expected_qualities

from vg_worker.stages import StageContext

log = get_logger("vg_worker.stages.ladder")

# Same shape vg_pipeline.ladder uses for injectable subprocess runners.
RunLike = Callable[..., Any]


@dataclass
class LadderResult:
    """Everything the ladder stage produced locally."""

    qualities: list[Rendition]
    audio_path: Path | None
    thumbs: list[Path]
    encoded: dict[tuple[str, str], Path]


def detect_nvenc(ffmpeg_bin: str, runner: RunLike) -> bool:
    """True when ``ffmpeg -encoders`` lists ``h264_nvenc``; else False.

    A runner that fails (missing binary, probe error) means "not available";
    a later encode attempt then fails closed with ENCODER_UNAVAILABLE if the
    CPU path also fails.
    """
    try:
        completed = runner([ffmpeg_bin, "-hide_banner", "-encoders"])
    except Exception:  # noqa: BLE001 — treat any failure as "no NVENC"
        return False
    if getattr(completed, "returncode", 1) != 0:
        return False
    return " h264_nvenc " in (getattr(completed, "stdout", "") or "")


def _encode_one(
    src: Path,
    out: Path,
    rendition: Rendition,
    probe: ProbeResult,
    ctx: StageContext,
    use_nvenc: bool,
) -> None:
    """Encode one rung, retrying once on the CPU path when NVENC fails.

    A missing binary or a zero-byte output fails closed with
    ``ENCODER_UNAVAILABLE`` (deterministic -> DLQ per CONTRACT.md section 2).
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    assert ctx.ffmpeg_runner is not None
    cmd = build_ffmpeg_command(
        src=src,
        out=out,
        r=rendition,
        fps=probe.fps,
        cfg=ctx.media,
        ffmpeg_bin=ctx.aws.bins.ffmpeg,
        has_audio=probe.has_audio,
        use_nvenc=use_nvenc,
    )
    runner: RunLike = ctx.ffmpeg_runner
    try:
        completed = runner(cmd)
    except OSError as exc:
        raise PipelineError("ENCODER_UNAVAILABLE", f"cannot run {ctx.aws.bins.ffmpeg}: {exc}") from exc
    stderr_tail = (getattr(completed, "stderr", "") or "").strip()[-400:]
    if getattr(completed, "returncode", 1) != 0:
        if not use_nvenc:
            raise PipelineError("ENCODER_UNAVAILABLE", f"libx264 encode of {rendition.name} failed: {stderr_tail}")
        log.warning("NVENC encode failed, retrying with libx264", extra={"quality": rendition.name})
        return _encode_one(src, out, rendition, probe, ctx, use_nvenc=False)
    if not out.is_file() or out.stat().st_size == 0:
        raise PipelineError("ENCODER_UNAVAILABLE", f"encode of {rendition.name} produced no output")


def run_ladder(ctx: StageContext, probe: ProbeResult) -> LadderResult:
    """Encode all produced qualities locally for variants a and b.

    Returns the local artifact paths; publishing happens after the Shaka
    encryption run in the package stage.
    """
    ctx.stage = "rendition_ready"
    qualities = expected_qualities(probe, ctx.media)
    if not qualities:
        raise PipelineError(
            "INPUT_CONTRACT",
            f"no ladder rung fits the source resolution {probe.width}x{probe.height}",
        )
    use_nvenc = detect_nvenc(ctx.aws.bins.ffmpeg, ctx.ffmpeg_runner)  # type: ignore[arg-type]

    encoded_root = ctx.work / "encoded"
    encoded: dict[tuple[str, str], Path] = {}
    for variant in ("a", "b"):
        master = ctx.masters.get(variant)
        if master is None or not master.is_file():
            raise PipelineError("ENCODER_UNAVAILABLE", f"missing {variant} master for the ladder")
        for rendition in qualities:
            out = encoded_root / variant / f"{rendition.name}.mp4"
            _encode_one(master, out, rendition, probe, ctx, use_nvenc)
            encoded[(variant, rendition.name)] = out

    audio = extract_audio(
        ctx.masters["a"],
        ctx.work / "audio.m4a",
        ctx.aws.bins.ffmpeg,
        run=_run_passthrough(ctx.ffmpeg_runner),
    )
    thumbs = extract_thumbnails(
        ctx.work / "input" / "source.mp4",
        ctx.work / "thumbs",
        count=ctx.media.thumbnail_count,
        duration_sec=probe.duration_sec,
        ffmpeg_bin=ctx.aws.bins.ffmpeg,
        run=_run_passthrough(ctx.ffmpeg_runner),
    )
    log.info(
        "ladder encoded",
        extra={"qualities": [r.name for r in qualities], "has_audio": audio is not None, "thumbs": len(thumbs)},
    )
    ctx.audio_path = audio
    ctx.thumbs = list(thumbs)
    ctx.encoded = dict(encoded)
    return LadderResult(
        qualities=list(qualities), audio_path=audio, thumbs=list(thumbs), encoded=dict(encoded)
    )


def _run_passthrough(runner: "RunLike | None") -> RunLike:
    """Wrap the injected runner so vg_pipeline can call it as ``run=``.

    vg_pipeline's helpers call ``run(cmd, capture_output=True, text=True)``;
    the worker's runner protocol is a plain callable of the argv, so the
    keyword arguments are accepted and ignored here. ``None`` maps to the
    plain ``subprocess.run`` (production default).
    """
    if runner is None:
        return subprocess.run

    def runner_with_kwargs(cmd: list[str], **_kwargs: object) -> object:
        return runner(cmd)

    return runner_with_kwargs
