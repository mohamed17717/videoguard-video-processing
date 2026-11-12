"""``done`` stage: tag the original, write the provenance manifest, build the
``done`` webhook payload (and the ``_DONE`` marker body).

``retention=archive`` adds the ``processed=true`` tag to the ORIGINAL in
``incoming/`` (its lifecycle then moves it to Glacier IR). ``retention=ttl30``
NEVER sets that tag (the object must expire after 30 days). Both cases carry
``vg-retention=<retention>``.

The manifest pins provenance and digests but NEVER the content key or IV:
``build_manifest_json`` (vg_pipeline) only accepts digests and versions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from vg_pipeline.artifacts import build_manifest_json, manifest_json_bytes
from vg_pipeline.contracts import ProbeResult, Rendition
from vg_pipeline.logging import get_logger

from vg_worker.s3io import make_tags
from vg_worker.stages import StageContext, streaming_sha256
from vg_worker.stages.package import PackageResult

log = get_logger("vg_worker.stages.finalize")


@dataclass
class FinalizeResult:
    """What finalize produced: the manifest key + the ``done`` payload."""

    manifest_key: str
    done_payload: dict


def run_finalize(
    ctx: StageContext,
    probe: ProbeResult,
    qualities: list[Rendition],
    package: PackageResult,
    *,
    watermark_version: str,
    completed_at: str,
) -> FinalizeResult:
    """Tag the original, upload ``manifest.json`` and build the done payload.

    Emits nothing itself: the caller (main) POSTs ``done`` with the returned
    payload, then writes ``_DONE``, then deletes the message.
    """
    ctx.stage = "done"
    retention = ctx.job.retention

    # Tag the ORIGINAL: processed=true ONLY for archive (CONTRACT.md section 1).
    tags = make_tags(retention, processed=(retention == "archive"))
    ctx.s3.put_tagging(ctx.incoming_key(), tags)

    # Artifact digests over the exact uploaded bytes. Masters are hashed from
    # the local generator output (the same bytes that were published).
    artifact_sha256: dict[str, str] = dict(package.artifact_sha256)
    for variant in ("a", "b"):
        master_path = ctx.masters.get(variant)
        if master_path is None:
            raise RuntimeError(f"{variant} master is missing from the stage context")
        master_key = ctx.key("masters", f"variant_{variant}.mp4")
        artifact_sha256[master_key] = streaming_sha256(master_path)

    document = build_manifest_json(
        job_id=ctx.job.job_id,
        qualities=[r.name for r in qualities],
        segment_counts=package.segment_counts,
        cfg=ctx.media,
        keys=package.keys,
        has_audio=bool(ctx.audio_path),
        retention=retention,
        source_sha256=ctx.source_sha256,
        artifact_sha256=artifact_sha256,
        generator_git_sha=str(ctx.completion.get("git_commit", "")),
        generator_image_digest=str(ctx.completion.get("image_digest") or ctx.generator_image),
        packager_version=ctx.packager_version,
        ffmpeg_version=ctx.ffmpeg_version,
        ami_id=ctx.ami_id,
        watermark_secret_version=watermark_version,
        completed_at=completed_at,
    )
    manifest_key = ctx.key("reports", "manifest.json")
    payload_bytes = manifest_json_bytes(document)
    ctx.s3.put_object(
        manifest_key,
        payload_bytes,
        storage_class=ctx.class_of("standard"),
        tags=make_tags(retention),
    )
    document["artifacts"][manifest_key] = hashlib.sha256(payload_bytes).hexdigest()

    thumbnail_keys = sorted(
        key for key in package.published_bytes if "/thumbs/thumb_" in key
    )
    done_payload = {
        "qualities": list(document["qualities"]),
        "hls_key": package.hls_key,
        "dash_key": package.dash_key,
        "thumbnail_keys": thumbnail_keys,
        "duration_seconds": round(probe.duration_sec, 3),
        # Delivery objects alone systematically understate the stored
        # footprint: the original upload and both retained masters are also
        # billed. The quota confirmation uses this number.
        "storage_bytes": (
            sum(package.published_bytes.values())
            + int(ctx.job.size_bytes)
            + sum(p.stat().st_size for p in ctx.masters.values() if p.is_file())
        ),
        "sha256": ctx.source_sha256,
        "watermark_secret_version": watermark_version,
        "retention": retention,
        "manifest_key": manifest_key,
    }
    log.info(
        "finalized",
        extra={"job_id": ctx.job.job_id, "manifest_key": manifest_key, "storage_bytes": done_payload["storage_bytes"]},
    )
    return FinalizeResult(manifest_key=manifest_key, done_payload=done_payload)
