"""Ladder encoding with FFmpeg: NVENC GPU path, CPU fallback, audio, thumbs.

Output codec for v1 is H.264 (CONTRACT.md section 4): ``h264_nvenc`` when the
GPU encoder is available, ``libx264`` from the same pinned FFmpeg otherwise.
Rate-control rules:

* NVENC uses ``-rc vbr -cq <cq>``; the CPU path uses ``-crf``.
* ``-crf`` and ``-cq`` are NEVER both passed.
* GOP is ``round(fps * gop_seconds)`` with scene-cut detection off, plus a
  forced key frame every ``gop_seconds`` so HLS/DASH segments align to GOPs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from .contracts import PipelineConfig, Rendition
from .errors import PipelineError
from .logging import get_logger

log = get_logger("ladder")

RunLike = Callable[..., subprocess.CompletedProcess]

# How many seconds of each segment's start to place thumbnails at. Spreading
# them over ``(i + 0.5) / count`` avoids frame 0 (often black) and the very
# end of the file.
_SCALE_FILTER = (
    "scale=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p"
)


def cpu_crf(rendition: Rendition) -> int:
    """Map the NVENC CQ of a rung to an x264 CRF for the CPU fallback.

    CQ and CRF are both quality-based rate controls on a roughly comparable
    0..51 scale (higher = lower quality). ``cq - 2`` reproduces the CRF the
    historical CPU ladder used at 720p (CQ 23 -> CRF 21). Clamped to the
    valid x264 range.
    """
    return max(0, min(51, rendition.nvenc_cq - 2))


def _gop(fps: float, cfg: PipelineConfig) -> int:
    return max(1, round(fps * cfg.gop_seconds))


def _fmt_fps(fps: float) -> str:
    return f"{fps:g}"


def build_ffmpeg_command(
    src: Path,
    out: Path,
    r: Rendition,
    fps: float,
    cfg: PipelineConfig,
    ffmpeg_bin: str,
    has_audio: bool,
    use_nvenc: bool = True,
) -> list[str]:
    """Build one FFmpeg encode command for one ladder rung.

    ``use_nvenc=True`` selects ``h264_nvenc`` with ``-rc vbr -cq``. The CPU
    fallback selects ``libx264`` with ``-crf`` and never passes ``-cq`` or the
    NVENC-only ``-rc`` flag. Audio is re-encoded to stereo AAC 128 kbit/s at
    48 kHz only when the source has audio.
    """
    gop = _gop(fps, cfg)
    cmd: list[str] = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(src),
        "-vf",
        _SCALE_FILTER.format(w=r.width, h=r.height),
    ]
    if use_nvenc:
        cmd += [
            "-c:v",
            "h264_nvenc",
            "-rc",
            "vbr",
            "-cq",
            str(r.nvenc_cq),
            "-b:v",
            f"{r.target_kbps}k",
        ]
    else:
        cmd += [
            "-c:v",
            "libx264",
            "-crf",
            str(cpu_crf(r)),
            "-b:v",
            f"{r.target_kbps}k",
        ]
    cmd += [
        "-maxrate",
        f"{r.maxrate_kbps}k",
        "-bufsize",
        f"{r.bufsize_kbps}k",
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{cfg.gop_seconds})",
        "-fps_mode",
        "cfr",
        "-r",
        _fmt_fps(fps),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ]
    if has_audio:
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
    cmd.append(str(out))
    return cmd


def _run_or_raise(cmd: list[str], run: RunLike, what: str) -> subprocess.CompletedProcess:
    """Run an FFmpeg command and turn tool failures into PipelineError.

    A missing binary and a nonzero exit both raise ``ENCODER_UNAVAILABLE``:
    the fixed error-code list has no broader encoder code, and the message
    carries the real cause.
    """
    try:
        completed = run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise PipelineError("ENCODER_UNAVAILABLE", f"cannot run {cmd[0]} for {what}: {exc}") from exc
    code = getattr(completed, "returncode", None)
    if code != 0:
        stderr = (getattr(completed, "stderr", "") or "").strip()
        raise PipelineError("ENCODER_UNAVAILABLE", f"{what} failed with exit {code}: {stderr[-500:]}")
    return completed


def extract_audio(src: Path, out: Path, ffmpeg_bin: str, *, run: RunLike = subprocess.run) -> Path | None:
    """Extract one shared AAC audio track from ``src``.

    Returns ``out`` on success and ``None`` when the source has no audio
    stream at all (ffmpeg prints "does not contain any stream"). Any other
    failure raises :class:`PipelineError`. The caller should skip the whole
    audio tree when ``None`` is returned (CONTRACT.md section 4).
    """
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(out),
    ]
    log.debug("audio extraction start", extra={"src": str(src)})
    try:
        completed = run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise PipelineError("ENCODER_UNAVAILABLE", f"cannot run {ffmpeg_bin} for audio extraction: {exc}") from exc
    if getattr(completed, "returncode", 0) != 0:
        stderr = (getattr(completed, "stderr", "") or "").strip()
        if "does not contain any stream" in stderr:
            # ffmpeg cannot build an output file with zero streams, which is
            # exactly the no-audio-source case.
            log.info("source has no audio stream", extra={"src": str(src)})
            return None
        raise PipelineError(
            "ENCODER_UNAVAILABLE",
            f"audio extraction failed with exit {completed.returncode}: {stderr[-500:]}",
        )
    log.info("audio extracted", extra={"out": str(out)})
    return out


def extract_thumbnails(
    src: Path,
    out_dir: Path,
    count: int,
    duration_sec: float,
    ffmpeg_bin: str,
    *,
    run: RunLike = subprocess.run,
) -> list[Path]:
    """Extract ``count`` JPEG thumbnails evenly spread over the duration.

    Timestamps sit at the middle of each of ``count`` equal slices, so no
    thumbnail lands exactly on frame 0 or on the last frame. Files are named
    ``thumb_01.jpg`` .. ``thumb_NN.jpg`` (CONTRACT.md section 1 layout). With
    ``count == 0`` nothing runs and the list is empty.
    """
    if count <= 0:
        return []
    if duration_sec <= 0:
        raise PipelineError("INPUT_CONTRACT", f"cannot place thumbnails on duration {duration_sec}")
    # The caller passes a per-job directory that does not exist yet; FFmpeg
    # fails with "No such file or directory" when the parent is missing.
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs: list[Path] = []
    for index in range(count):
        stamp = duration_sec * (index + 0.5) / count
        out = out_dir / f"thumb_{index + 1:02d}.jpg"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-ss",
            f"{stamp:.6f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(out),
        ]
        _run_or_raise(cmd, run, f"thumbnail {out.name}")
        thumbs.append(out)
    log.info("thumbnails extracted", extra={"count": len(thumbs), "src": str(src)})
    return thumbs
