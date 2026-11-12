"""Shaka Packager packaging and the aligned-timeline gate.

ONE packager run with 9 stream descriptors (CONTRACT.md section 6, D2
design): 8 video (variants a/b times the produced qualities) and 1 audio,
all encrypted with the same key/IV. Shaka's own master playlist and MPD are
written to temp paths and discarded; the canonical manifests come from
:mod:`vg_pipeline.artifacts` so the A/B variants are never published as 8
public variants.

Two separate packager runs cannot produce byte-identical raw ``init.mp4``
files because Shaka's MP4 muxer writes wall-clock timestamps, so the gate
compares CANONICALISED inits (:func:`vg_pipeline.mp4.canonicalize_init`) plus
segment counts and per-index decode times.
"""

from __future__ import annotations

from pathlib import Path

from .contracts import AlignedTimeline, CencKeys, PipelineConfig, StreamSpec
from .errors import PipelineError
from .logging import get_logger
from .mp4 import _read_tfdt_bytes, canonicalize_init

log = get_logger("shaka")

# Temp paths inside the packager output dir; their content is discarded.
_SHAKA_HLS_TMP = "_shaka_master.m3u8"
_SHAKA_DASH_TMP = "_shaka_stream.mpd"

_SEGMENT_GLOB = "seg_*.m4s"


def _descriptor(spec: StreamSpec) -> str:
    """One Shaka stream descriptor string for one input."""
    return (
        f"input={spec.input},stream={spec.stream},"
        f"init_segment={spec.init_segment},"
        f"segment_template={spec.segment_template},"
        f"playlist_name={spec.playlist_name}"
    )


def build_packager_args(
    streams: list[StreamSpec],
    keys: CencKeys,
    cfg: PipelineConfig,
    out_dir: Path,
    packager_bin: str,
) -> list[str]:
    """Build the full argument list for ONE packager run.

    Returns ``[packager_bin, ...]`` followed by all stream descriptors and
    the flags from CONTRACT.md section 6. All streams share the same
    ``--keys``/``--iv``/``--pssh``. The generated master playlist and MPD go
    to temp paths inside ``out_dir`` and are discarded.
    """
    args: list[str] = [packager_bin]
    for spec in streams:
        args.append(_descriptor(spec))
    args += [
        "--segment_duration",
        str(cfg.segment_duration_sec),
        "--generate_static_live_mpd",
        "--protection_systems=Widevine,PlayReady,FairPlay",
        "--clear_lead=0",
        # cfg.drm.scheme is "cbcs"; pass it through so config stays the source.
        f"--protection_scheme={cfg.drm.get('scheme', 'cbcs')}",
        "--enable_raw_key_encryption",
        f"--keys=key_id={keys.key_id_hex}:key={keys.key_hex}",
        f"--iv={keys.iv_hex}",
        f"--pssh={keys.pssh_hex}",
        f"--hls_key_uri={keys.skd_uri}",
        "--hls_master_playlist_output",
        str(out_dir / _SHAKA_HLS_TMP),
        "--mpd_output",
        str(out_dir / _SHAKA_DASH_TMP),
    ]
    log.info(
        "packager command built",
        extra={"streams": len(streams), "segment_duration": cfg.segment_duration_sec},
    )
    return args


def _segment_number(path: Path) -> int:
    """Numeric index of ``seg_<n>.m4s`` (Shaka emits unpadded names)."""
    try:
        return int(path.stem.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0


def _segment_paths(quality_dir: Path) -> list[Path]:
    """Sorted ``seg_*.m4s`` paths of one quality directory (numeric order)."""
    return sorted(
        (p for p in quality_dir.glob(_SEGMENT_GLOB) if p.is_file()),
        key=_segment_number,
    )


def _read_playlist_durations(path: Path) -> list[float]:
    """Per-segment durations from a Shaka HLS media playlist (``#EXTINF``).

    Shaka writes the authoritative durations (including the final partial
    segment and fractional-frame-rate boundaries) into its own playlist.
    Those playlists are not published, but they are read before being
    discarded so the canonical manifests carry real durations.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    values: list[float] = []
    for line in text.splitlines():
        if not line.startswith("#EXTINF:"):
            continue
        value = line[len("#EXTINF:"):].split(",", 1)[0].strip()
        try:
            values.append(float(value))
        except ValueError:
            return []
    return values


def check_aligned(a_dir: Path, b_dir: Path, qualities: list[str]) -> AlignedTimeline:
    """Run the aligned-timeline gate between variant A and variant B trees.

    All three conditions must pass, else the job fails with
    ``PACKAGING_INIT_MISMATCH`` and there is NO fallback (CONTRACT.md
    section 6):

    1. equal segment count per quality;
    2. equal ``tfdt`` baseMediaDecodeTime for each segment index;
    3. byte-identical canonicalised ``init.mp4`` per quality (raw inits may
       differ by wall-clock timestamps written by the muxer).
    """
    segment_counts: dict[str, int] = {}
    init_identical = True
    tfdt_aligned = True
    problems: list[str] = []

    for quality in qualities:
        a_quality = a_dir / quality
        b_quality = b_dir / quality
        a_segments = _segment_paths(a_quality)
        b_segments = _segment_paths(b_quality)
        if len(a_segments) != len(b_segments):
            problems.append(
                f"{quality}: segment count mismatch a={len(a_segments)} b={len(b_segments)}"
            )
            tfdt_aligned = False
            continue
        segment_counts[quality] = len(a_segments)

        # Condition 3: canonicalised init bytes.
        a_init_path = a_quality / "init.mp4"
        b_init_path = b_quality / "init.mp4"
        try:
            a_init = canonicalize_init(a_init_path.read_bytes())
            b_init = canonicalize_init(b_init_path.read_bytes())
        except OSError as exc:
            problems.append(f"{quality}: cannot read init segments: {exc}")
            init_identical = False
            continue
        if a_init != b_init:
            problems.append(f"{quality}: canonicalised init.mp4 differs between a and b")
            init_identical = False

        # Condition 2: decode time of each segment index.
        for a_seg, b_seg in zip(a_segments, b_segments):
            a_tfdt = _read_tfdt_bytes(a_seg.read_bytes())
            b_tfdt = _read_tfdt_bytes(b_seg.read_bytes())
            if a_tfdt is None or b_tfdt is None or a_tfdt != b_tfdt:
                problems.append(
                    f"{quality}/{a_seg.name}: tfdt mismatch a={a_tfdt} b={b_tfdt}"
                )
                tfdt_aligned = False
                break

    segment_durations: dict[str, list[float]] = {}
    for quality in qualities:
        values = _read_playlist_durations(a_dir.parent / f"_a_{quality}.m3u8")
        if values:
            segment_durations[quality] = values
    audio_durations = _read_playlist_durations(a_dir.parent / "_audio.m3u8")

    if problems:
        log.error("aligned-timeline gate failed", extra={"qualities": qualities})
        raise PipelineError("PACKAGING_INIT_MISMATCH", "; ".join(problems))
    log.info(
        "aligned-timeline gate passed",
        extra={"qualities": qualities, "segment_counts": segment_counts},
    )
    return AlignedTimeline(
        segment_counts=segment_counts,
        init_identical=init_identical,
        tfdt_aligned=tfdt_aligned,
        segment_durations=segment_durations,
        audio_durations=audio_durations,
    )
