"""``packaged`` stage: keys, ONE packager run, canonicalisation, publishing.

Order matters here (CONTRACT.md sections 3, 6, 8):

1. ONE Axinom key request (the content id comes from the sidecar);
2. ONE Shaka Packager run with the 9 stream descriptors: 8 video
   (variants a/b times the produced qualities) plus the audio, all sharing
   the same ``--keys``/``--iv``. Shaka's own master playlist and MPD are
   written to temp paths and discarded;
3. the aligned-timeline gate (equal segment counts per quality, equal tfdt
   decode times per index, byte-identical normalised inits) — failure is
   ``PACKAGING_INIT_MISMATCH`` with NO fallback;
4. init canonicalisation (zero creation/modification times with the MP4 box
   walker) and ONE canonical init per quality under
   ``renditions/<job>/init/<quality>/init.mp4`` — shared by A and B;
5. per quality: upload the encrypted A and B segments to
   ``renditions/<job>/<variant>/<quality>/seg_NNNNN.m4s``, ``head_object``
   BOTH keys, then emit ``rendition_ready`` (qualifier = quality) — AFTER
   the successful PutObject + HeadObject check, exactly as CONTRACT.md
   section 3 requires;
6. audio inits + segments, thumbnails, canonical manifest bytes, then the
   ``packaged`` webhook.

Output layout note: the packager descriptor writes ``init_segment`` and
``segment_template`` INSIDE ``<work>/packaged`` — Shaka writes the encrypted
segments there, reading the input files the ladder encoded. The same
descriptor shape was used by the historical Go worker
(``internal/transcode/shaka.go``), which set them to ``OutDir/...`` paths.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from vg_pipeline.artifacts import build_canonical_manifests
from vg_pipeline.contracts import (
    AlignedTimeline,
    AxinomSecrets,
    CencKeys,
    ProbeResult,
    Rendition,
    StreamSpec,
)
from vg_pipeline.cenc import request_cenc_keys
from vg_pipeline.errors import PipelineError
from vg_pipeline.logging import get_logger
from vg_pipeline.mp4 import canonicalize_init
from vg_pipeline.shaka import build_packager_args, check_aligned

from vg_worker.s3io import make_tags
from vg_worker.stages import StageContext

log = get_logger("vg_worker.stages.package")


@dataclass
class PackageResult:
    """Everything the package stage published, for the finalize stage."""

    keys: CencKeys
    qualities: list[str]
    segment_counts: dict[str, int]
    hls_key: str
    dash_key: str
    init_keys: dict[str, str]
    audio_init_key: "str | None"
    audio_key: "str | None"
    thumb_keys: list[str] = field(default_factory=list)
    published_bytes: dict[str, int] = field(default_factory=dict)
    artifact_sha256: dict[str, str] = field(default_factory=dict)


def _stream_specs(ctx: StageContext, qualities: list[Rendition], packaged: Path) -> list[StreamSpec]:
    """The 8 video (+1 audio, when present) stream descriptors."""
    specs: list[StreamSpec] = []
    for variant in ("a", "b"):
        master = ctx.masters.get(variant)
        if master is None or not master.is_file():
            raise PipelineError("MANIFEST_FAILED", f"missing {variant} master before packaging")
        for rendition in qualities:
            tree = packaged / variant / rendition.name
            encoded = ctx.encoded.get((variant, rendition.name))
            if encoded is None or not encoded.is_file():
                raise PipelineError(
                    "MANIFEST_FAILED",
                    f"missing encoded {variant}/{rendition.name} before packaging",
                )
            specs.append(
                StreamSpec(
                    input=encoded,
                    stream="video",
                    init_segment=tree / "init.mp4",
                    segment_template=tree / "seg_$Number$.m4s",
                    playlist_name=str(packaged / f"_{variant}_{rendition.name}.m3u8"),
                    variant=variant,
                    quality=rendition.name,
                )
            )
    if ctx.audio_path is not None:
        tree = packaged / "audio"
        specs.append(
            StreamSpec(
                input=ctx.audio_path,
                stream="audio",
                init_segment=tree / "init.mp4",
                segment_template=tree / "seg_$Number$.m4s",
                playlist_name=str(packaged / "_audio.m3u8"),
                variant="audio",
                quality="audio",
            )
        )
    return specs


def _segment_number(path: Path) -> int:
    """Numeric index of ``seg_<n>.m4s``; Shaka emits unpadded names."""
    try:
        return int(path.stem.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0


def _sorted_segments(directory: Path) -> list[Path]:
    """Segment files of one quality tree, in DECODE order.

    Shaka is configured with unpadded ``seg_$Number$.m4s`` names, so a
    lexicographic sort would produce 1, 10, 11, ..., 2. Sort by the
    numeric index; publication renames the files to padded names.
    """
    return sorted(
        (p for p in directory.glob("seg_*.m4s") if p.is_file()),
        key=_segment_number,
    )


class _Publisher:
    """Upload helper that records every rendition-side object's byte count."""

    def __init__(self, ctx: StageContext, result: "PackageResult") -> None:
        self._ctx = ctx
        self._result = result

    def put(self, key: str, payload: bytes, storage_class: str, tags: dict[str, str]) -> None:
        """Write one object and track size + SHA-256 in the result."""
        self._ctx.s3.put_object(key, payload, storage_class=storage_class, tags=tags)
        self._result.published_bytes[key] = len(payload)
        self._result.artifact_sha256[key] = hashlib.sha256(payload).hexdigest()

    def publish_audio_tree(self, directory: Path, key_prefix: str, storage_class: str, tags: dict[str, str]) -> int:
        """Upload the canonical audio init + all audio segments; returns bytes.

        The init is canonicalised (creation/modification times zeroed) exactly
        like the video inits. Segment file names are normalised to
        ``seg_NNNNN.m4s`` because the canonical playlists reference that
        exact pattern.
        """
        uploaded = 0
        init_canonical = canonicalize_init((directory / "init.mp4").read_bytes())
        self.put(f"{key_prefix}init.mp4", init_canonical, storage_class, tags)
        uploaded += len(init_canonical)
        for index, segment in enumerate(_sorted_segments(directory), start=1):
            payload = segment.read_bytes()
            self.put(f"{key_prefix}seg_{index:05d}.m4s", payload, storage_class, tags)
            uploaded += len(payload)
        return uploaded


def run_package(
    ctx: StageContext,
    probe: ProbeResult,
    qualities: list[Rendition],
    axinom: AxinomSecrets,
) -> PackageResult:
    """Run the whole packaging flow and emit ``rendition_ready`` per quality.

    Raises ``PACKAGING_INIT_MISMATCH`` (deterministic DLQ) when the gate
    fails, and ``MANIFEST_FAILED`` for a crashed packager.
    """
    ctx.stage = "packaged"
    if not qualities:
        raise PipelineError("MANIFEST_FAILED", "no qualities reached the packager")
    quality_names = [r.name for r in qualities]

    packaged = ctx.work / "packaged"
    packaged.mkdir(parents=True, exist_ok=True)
    specs = _stream_specs(ctx, qualities, packaged)

    log.info("requesting CENC keys", extra={"job_id": ctx.job.job_id, "content_scheme": axinom.scheme})
    keys = request_cenc_keys(
        content_id=ctx.job.content_id,
        secrets=axinom,
        http_post=ctx.axinom_post,
    )
    args = build_packager_args(
        streams=specs,
        keys=keys,
        cfg=ctx.media,
        out_dir=packaged,
        packager_bin=ctx.aws.bins.packager,
    )
    if ctx.packager_runner is None:
        raise PipelineError("MANIFEST_FAILED", "no packager runner injected")
    completed = ctx.packager_runner(args)
    if getattr(completed, "returncode", 1) != 0:
        stderr_tail = (getattr(completed, "stderr", "") or "").strip()[-400:]
        raise PipelineError(
            "MANIFEST_FAILED",
            f"packager exited {getattr(completed, 'returncode', -1)}: {stderr_tail}",
        )

    gate: AlignedTimeline = check_aligned(packaged / "a", packaged / "b", quality_names)
    counts = gate.segment_counts
    log.info("aligned-timeline gate passed", extra={"segment_counts": counts})

    retention = ctx.job.retention
    tags = make_tags(retention)
    cmaf_class = ctx.class_of("cmaf")
    standard_class = ctx.class_of("standard")
    package_result = PackageResult(
        keys=keys,
        qualities=list(quality_names),
        segment_counts=dict(counts),
        hls_key=f"renditions/{ctx.job.job_id}/hls/master.m3u8",
        dash_key=f"renditions/{ctx.job.job_id}/dash/stream.mpd",
        init_keys={},
        audio_init_key=None,
        audio_key=None,
    )
    publisher = _Publisher(ctx, package_result)

    # 4. ONE canonical init per quality, uploaded BEFORE any segment so a
    # player that loads init first never 404s.
    for quality in quality_names:
        init_path = packaged / "a" / quality / "init.mp4"
        canonical = canonicalize_init(init_path.read_bytes())
        init_key = ctx.key("renditions", "init", quality, "init.mp4")
        publisher.put(init_key, canonical, cmaf_class, tags)
        package_result.init_keys[quality] = init_key

    # 5. per-quality encrypted segments for BOTH variants, HEAD-check, webhook.
    for quality in quality_names:
        uploaded: dict[str, int] = {}
        keys_by_variant: dict[str, str] = {}
        for variant in ("a", "b"):
            tree = packaged / variant / quality
            segments = _sorted_segments(tree)
            if not segments:
                raise PipelineError("MANIFEST_FAILED", f"no segments for {variant}/{quality} after the gate")
            variant_prefix = ctx.key("renditions", variant, quality, "")
            for index, segment in enumerate(segments, start=1):
                seg_key = f"{variant_prefix}seg_{index:05d}.m4s"
                payload = segment.read_bytes()
                publisher.put(seg_key, payload, cmaf_class, tags)
                uploaded[variant] = uploaded.get(variant, 0) + len(payload)
                keys_by_variant.setdefault(variant, seg_key)
            # HeadObject of the first uploaded segment of BOTH variants
            # (CONTRACT.md section 3: rendition_ready only after upload AND a
            # HEAD/ETag check for a+b).
            try:
                ctx.s3.client.head_object(Bucket=ctx.s3.bucket, Key=f"{variant_prefix}seg_00001.m4s")
            except Exception as exc:  # noqa: BLE001 — the object vanished right after PUT
                raise PipelineError(
                    "MANIFEST_FAILED",
                    f"head_object failed for {variant}/{quality} ({type(exc).__name__})",
                ) from exc
        ctx.emitter.emit(
            "rendition_ready",
            qualifier=quality,
            payload={"quality": quality, "bytes": uploaded, "keys": keys_by_variant},
        )

    # 6. audio tree (shared), thumbnails, canonical manifests.
    if ctx.audio_path is not None:
        audio_tree = packaged / "audio"
        audio_prefix = ctx.key("renditions", "audio", "")
        uploaded_audio = publisher.publish_audio_tree(audio_tree, audio_prefix, cmaf_class, tags)
        package_result.audio_init_key = f"{audio_prefix}init.mp4"
        log.info("audio published", extra={"bytes": uploaded_audio})
    thumb_key_list: list[str] = []
    for thumb in ctx.thumbs:
        thumb_key = ctx.key("renditions", "thumbs", thumb.name)
        payload = thumb.read_bytes()
        publisher.put(thumb_key, payload, standard_class, tags)
        thumb_key_list.append(thumb_key)
    manifest_set = build_canonical_manifests(
        job_id=ctx.job.job_id,
        out_dir=ctx.work / "manifests",
        qualities=quality_names,
        segment_counts=counts,
        cfg=ctx.media,
        keys=keys,
        has_audio=bool(ctx.audio_path),
        durations=gate.segment_durations,
        audio_durations=gate.audio_durations,
    )
    for manifest_key, payload in sorted(manifest_set.files.items()):
        publisher.put(manifest_key, payload, standard_class, tags)
    if package_result.hls_key not in manifest_set.files or package_result.dash_key not in manifest_set.files:
        raise PipelineError("MANIFEST_FAILED", "canonical manifests are missing the hls/dash keys")

    ctx.emitter.emit(
        "packaged",
        payload={
            "hls_key": package_result.hls_key,
            "dash_key": package_result.dash_key,
            "segment_counts": counts,
            "aligned": {"init_identical": gate.init_identical, "tfdt_aligned": gate.tfdt_aligned},
            "init_keys": dict(package_result.init_keys),
        },
    )
    log.info("packaged", extra={"qualities": quality_names, "bytes": sum(package_result.published_bytes.values())})
    return package_result
