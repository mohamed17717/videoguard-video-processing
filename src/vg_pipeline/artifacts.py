"""Canonical manifests: HLS master/media playlists, DASH MPD, manifest.json.

Shaka's own master playlist and MPD are discarded (they would expose all 8
A/B combinations). These builders write the published versions instead
(CONTRACT.md sections 1, 6, 7):

* Segment URLs are root-relative and carry the literal token
  ``__VG_VARIANT__`` for video, e.g. ``/<job>/__VG_VARIANT__/720p/seg_00001.m4s``.
  The edge replaces the token per session. Init URLs have NO token:
  ``/<job>/init/720p/init.mp4`` is shared by both variants.
* Audio uses one shared tree, ``/<job>/audio/...``, no variant token.
* ``manifest.json`` records provenance and the key id, but NEVER the content
  key or IV.
"""

from __future__ import annotations

import base64
import json
import math
import os
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .contracts import CencKeys, ManifestSet, PipelineConfig
from .errors import PipelineError
from .logging import get_logger

log = get_logger("artifacts")

# Token the playback edge replaces with "a" or "b" per session.
VARIANT_TOKEN = "__VG_VARIANT__"

# Static estimate for the H.264 output (High Profile, Level 4.1). The encoder
# writes the real profile/level into the sample entries; this string only
# feeds player codec negotiation.
_VIDEO_CODEC_STRING = "avc1.640029"

# DRM system UUIDs for DASH ContentProtection elements.
_SYSTEM_NAMES: dict[str, str] = {
    "edef8ba979d64acea3c827dcd51d21ed": "widevine",
    "9a04f07998404286ab92e65be0885f95": "playready",
}
_PROTECTION_SCHEME_ID = "urn:mpeg:dash:mp4:protection:2011"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _s3_root(job_id: str) -> str:
    """Object-key root for all published renditions of one job."""
    return f"renditions/{job_id}"


def _hls_media_key(job_id: str, quality: str) -> str:
    return f"{_s3_root(job_id)}/hls/media_{quality}.m3u8"


def _hls_audio_key(job_id: str) -> str:
    return f"{_s3_root(job_id)}/hls/audio.m3u8"


def _hls_master_key(job_id: str) -> str:
    return f"{_s3_root(job_id)}/hls/master.m3u8"


def _dash_key(job_id: str) -> str:
    return f"{_s3_root(job_id)}/dash/stream.mpd"


def _playlist_header(target_duration_sec: int) -> list[str]:
    return [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target_duration_sec}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]


def _target_duration(durations: list[float], cfg: PipelineConfig) -> int:
    """HLS TARGETDURATION is the rounded-UP longest segment."""
    if durations:
        return max(1, math.ceil(max(durations)))
    return int(cfg.segment_duration_sec)


def _per_segment_durations(
    segment_count: int,
    cfg: PipelineConfig,
    durations: list[float] | None,
) -> list[float]:
    """One duration per published segment (real values when available)."""
    if durations and len(durations) == segment_count:
        return list(durations)
    return [float(cfg.segment_duration_sec)] * segment_count


def _fairplay_key_line(keys: CencKeys) -> str:
    """The FairPlay signalling Shaka would have written into its own playlist.

    Shaka's playlists are discarded, so the canonical playlist must carry the
    EXT-X-KEY itself or HLS clients never learn how to fetch the key.
    """
    if not keys.skd_uri:
        raise PipelineError("MANIFEST_FAILED", "FairPlay skd_uri is missing")
    return (
        f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{keys.skd_uri}",'
        'KEYFORMAT="com.apple.streamingkeydelivery",KEYFORMATVERSIONS="1"'
    )


def _media_playlist(
    job_id: str,
    quality: str,
    segment_count: int,
    cfg: PipelineConfig,
    keys: CencKeys,
    durations: list[float] | None = None,
) -> bytes:
    """One video media playlist for one quality of one variant."""
    if segment_count < 1:
        raise PipelineError("MANIFEST_FAILED", f"quality {quality} has {segment_count} segments")
    per_segment = _per_segment_durations(segment_count, cfg, durations)
    lines = _playlist_header(_target_duration(per_segment, cfg))
    lines.append(_fairplay_key_line(keys))
    lines.append(f'#EXT-X-MAP:URI="/{job_id}/init/{quality}/init.mp4"')
    for index, duration in enumerate(per_segment, start=1):
        lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(f"/{job_id}/{VARIANT_TOKEN}/{quality}/seg_{index:05d}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _audio_playlist(
    job_id: str,
    segment_count: int,
    cfg: PipelineConfig,
    keys: CencKeys,
    durations: list[float] | None = None,
) -> bytes:
    """One shared audio media playlist (no variant token anywhere)."""
    if segment_count < 1:
        raise PipelineError("MANIFEST_FAILED", f"audio has {segment_count} segments")
    per_segment = _per_segment_durations(segment_count, cfg, durations)
    lines = _playlist_header(_target_duration(per_segment, cfg))
    lines.append(_fairplay_key_line(keys))
    lines.append(f'#EXT-X-MAP:URI="/{job_id}/audio/init.mp4"')
    for index, duration in enumerate(per_segment, start=1):
        lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(f"/{job_id}/audio/seg_{index:05d}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _master_playlist(job_id: str, qualities: list[str], segment_counts: dict[str, int], cfg: PipelineConfig, has_audio: bool) -> bytes:
    """HLS master listing only the produced qualities."""
    if not qualities:
        raise PipelineError("MANIFEST_FAILED", "master playlist needs at least one quality")
    renditions_by_name = {r.name: r for r in cfg.ladder}
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    if has_audio:
        lines.append(
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="en",DEFAULT=YES,AUTOSELECT=YES,'
            f'URI="/{job_id}/hls/audio.m3u8"'
        )
    for quality in qualities:
        rendition = renditions_by_name.get(quality)
        if rendition is None:
            raise PipelineError("MANIFEST_FAILED", f"quality {quality!r} is not in the configured ladder")
        count = segment_counts.get(quality, 0)
        if count < 1:
            raise PipelineError("MANIFEST_FAILED", f"quality {quality} has {count} segments")
        attributes = [
            f"BANDWIDTH={rendition.maxrate_kbps * 1000}",
            f"AVERAGE-BANDWIDTH={rendition.target_kbps * 1000}",
            f"RESOLUTION={rendition.width}x{rendition.height}",
            f'CODECS="{_VIDEO_CODEC_STRING}"',
        ]
        if has_audio:
            attributes.append('AUDIO="audio"')
        lines.append(f"#EXT-X-STREAM-INF:{','.join(attributes)}")
        lines.append(f"/{job_id}/hls/media_{quality}.m3u8")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _split_pssh_boxes(pssh_hex: str) -> list[tuple[str, bytes]]:
    """Parse concatenated PSSH boxes into ``(system_id_hex, box_bytes)`` pairs."""
    boxes: list[tuple[str, bytes]] = []
    try:
        data = bytes.fromhex(pssh_hex)
    except ValueError as exc:
        raise PipelineError("MANIFEST_FAILED", f"invalid pssh hex: {exc}") from exc
    pos = 0
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos : pos + 4], "big")
        box_type = data[pos + 4 : pos + 8]
        if size < 8 or pos + size > len(data) or box_type != b"pssh":
            break
        version = data[pos + 8]
        if version == 0 and size >= 32:
            system_id = data[pos + 12 : pos + 28]
        elif version == 1 and size >= 44:
            system_id = data[pos + 28 : pos + 44]
        else:
            pos += size
            continue
        boxes.append((system_id.hex(), data[pos : pos + size]))
        pos += size
    if not boxes:
        raise PipelineError("MANIFEST_FAILED", "no parseable PSSH box in the combined pssh")
    return boxes


def _uuid_urn(system_id_hex: str) -> str:
    h = system_id_hex
    return f"urn:uuid:{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def build_dash_mpd(
    job_id: str,
    qualities: list[str],
    segment_counts: dict[str, int],
    cfg: PipelineConfig,
    keys: CencKeys,
    has_audio: bool,
    durations: dict[str, list[float]] | None = None,
    audio_durations: list[float] | None = None,
) -> bytes:
    """Build the canonical static DASH MPD as pretty-printed XML bytes."""
    if not qualities:
        raise PipelineError("MANIFEST_FAILED", "DASH MPD needs at least one quality")
    if not segment_counts or max(segment_counts.values()) < 1:
        raise PipelineError("MANIFEST_FAILED", f"no segments to publish: {segment_counts}")
    renditions_by_name = {r.name: r for r in cfg.ladder}
    protection_boxes = _split_pssh_boxes(keys.pssh_hex)
    per_quality = durations or {}
    if per_quality:
        presentation = max(sum(values) for values in per_quality.values() if values)
    else:
        presentation = max(segment_counts.values()) * cfg.segment_duration_sec

    mpd = ET.Element(
        "MPD",
        {
            "xmlns": "urn:mpeg:dash:schema:mpd:2011",
            "xmlns:cenc": "urn:mpeg:cenc:2013",
            "profiles": "urn:mpeg:dash:profile:isoff-live:2011",
            "type": "static",
            "mediaPresentationDuration": f"PT{presentation:g}S",
            "minBufferTime": "PT4S",
        },
    )
    comment = ET.Comment(
        f" The playback edge rewrites {VARIANT_TOKEN} to the session's variant "
        "and leaves the init path untouched. $Number%05d$ pads segment numbers "
        "to five digits (seg_00001.m4s). "
    )
    mpd.append(comment)
    period = ET.SubElement(mpd, "Period", {"id": "0", "start": "PT0S"})

    def protection_elements(parent: ET.Element) -> None:
        ET.SubElement(parent, "ContentProtection", {"schemeIdUri": _PROTECTION_SCHEME_ID, "value": "cbcS"})
        for system_id_hex, box in protection_boxes:
            name = _SYSTEM_NAMES.get(system_id_hex, "cenc")
            element = ET.SubElement(
                parent,
                "ContentProtection",
                {"schemeIdUri": _uuid_urn(system_id_hex), "value": name},
            )
            pssh = ET.SubElement(element, "{urn:mpeg:cenc:2013}pssh")
            pssh.text = _b64(box)

    for index, quality in enumerate(qualities):
        rendition = renditions_by_name.get(quality)
        if rendition is None:
            raise PipelineError("MANIFEST_FAILED", f"quality {quality!r} is not in the configured ladder")
        count = segment_counts.get(quality, 0)
        if count < 1:
            raise PipelineError("MANIFEST_FAILED", f"quality {quality} has {count} segments")
        adaptation = ET.SubElement(
            period,
            "AdaptationSet",
            {
                "id": str(index),
                "contentType": "video",
                "mimeType": "video/mp4",
                "segmentAlignment": "true",
                "startWithSAP": "1",
            },
        )
        protection_elements(adaptation)
        representation = ET.SubElement(
            adaptation,
            "Representation",
            {
                "id": quality,
                "bandwidth": str(rendition.maxrate_kbps * 1000),
                "width": str(rendition.width),
                "height": str(rendition.height),
                "codecs": _VIDEO_CODEC_STRING,
            },
        )
        quality_durations = per_quality.get(quality)
        template = ET.SubElement(
            representation,
            "SegmentTemplate",
            {
                "initialization": f"/{job_id}/init/{quality}/init.mp4",
                "media": f"/{job_id}/{VARIANT_TOKEN}/{quality}/seg_$Number%05d$.m4s",
                "startNumber": "1",
                **({"timescale": "1000"} if quality_durations else {
                    "duration": str(cfg.segment_duration_sec),
                    "timescale": "1",
                }),
            },
        )
        if quality_durations:
            timeline = ET.SubElement(template, "SegmentTimeline")
            for value in quality_durations:
                ET.SubElement(timeline, "S", {"d": str(max(1, round(value * 1000)))})

    if has_audio:
        # Use the audio tree's OWN timeline: audio and video segment counts can
        # differ at a partial final segment or a fractional frame-rate boundary.
        adaptation = ET.SubElement(
            period,
            "AdaptationSet",
            {
                "id": str(len(qualities)),
                "contentType": "audio",
                "mimeType": "audio/mp4",
                "segmentAlignment": "true",
                "startWithSAP": "1",
            },
        )
        protection_elements(adaptation)
        representation = ET.SubElement(
            adaptation,
            "Representation",
            {"id": "audio", "bandwidth": "128000", "audioSamplingRate": "48000"},
        )
        ET.SubElement(
            representation,
            "AudioChannelConfiguration",
            {
                "schemeIdUri": "urn:mpeg:dash:23003:3:audio_channel_configuration:2011",
                "value": "2",
            },
        )
        template = ET.SubElement(
            representation,
            "SegmentTemplate",
            {
                "initialization": f"/{job_id}/audio/init.mp4",
                "media": f"/{job_id}/audio/seg_$Number%05d$.m4s",
                "startNumber": "1",
                **({"timescale": "1000"} if audio_durations else {
                    "duration": str(cfg.segment_duration_sec),
                    "timescale": "1",
                }),
            },
        )
        if audio_durations:
            timeline = ET.SubElement(template, "SegmentTimeline")
            for value in audio_durations:
                ET.SubElement(timeline, "S", {"d": str(max(1, round(value * 1000)))})

    tree = ET.ElementTree(mpd)
    ET.indent(tree, space="  ")
    return ET.tostring(mpd, encoding="UTF-8", xml_declaration=True)


def build_manifest_json(
    job_id: str,
    qualities: list[str],
    segment_counts: dict[str, int],
    cfg: PipelineConfig,
    keys: CencKeys,
    has_audio: bool,
    *,
    retention: str = "archive",
    source_sha256: str = "",
    artifact_sha256: dict[str, str] | None = None,
    generator_git_sha: str = "",
    generator_image_digest: str = "",
    packager_version: str = "",
    ffmpeg_version: str = "",
    ami_id: str = "",
    watermark_secret_version: str = "",
    completed_at: str = "",
) -> dict:
    """Build the ``reports/<job_id>/manifest.json`` document (CONTRACT.md section 0).

    Pins provenance, the ladder, the key ID and per-artifact SHA-256 values.
    The content key and IV must NEVER be added here; callers pass digests and
    versions, and this builder ignores any field it does not know.
    """
    renditions_by_name = {r.name: r for r in cfg.ladder}
    ladder = [
        {
            "name": quality,
            "width": renditions_by_name[quality].width,
            "height": renditions_by_name[quality].height,
            "target_kbps": renditions_by_name[quality].target_kbps,
            "maxrate_kbps": renditions_by_name[quality].maxrate_kbps,
            "bufsize_kbps": renditions_by_name[quality].bufsize_kbps,
        }
        for quality in qualities
        if quality in renditions_by_name
    ]
    document = {
        "schema_version": 1,
        "job_id": job_id,
        "profile": cfg.profile,
        "qualities": list(qualities),
        "has_audio": has_audio,
        "ladder": ladder,
        "segment_counts": dict(segment_counts),
        "drm_scheme": cfg.drm.get("scheme", "cbcs"),
        "key_id": keys.key_id_hex,
        "generator_git_sha": generator_git_sha,
        "generator_image_digest": generator_image_digest,
        "packager_version": packager_version,
        "ffmpeg_version": ffmpeg_version,
        "ami_id": ami_id,
        "source_sha256": source_sha256,
        "artifacts": dict(artifact_sha256 or {}),
        "retention": retention,
        "watermark_secret_version": watermark_secret_version,
        "completed_at": completed_at,
    }
    return document


def _write_atomic(out_dir: Path, relative_key: str, payload: bytes) -> None:
    """Write ``payload`` to ``out_dir/<relative_key>`` via temp file + rename."""
    final = out_dir / relative_key
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f".{final.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, final)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def build_canonical_manifests(
    job_id: str,
    out_dir: Path,
    qualities: list[str],
    segment_counts: dict[str, int],
    cfg: PipelineConfig,
    keys: CencKeys,
    has_audio: bool,
    durations: dict[str, list[float]] | None = None,
    audio_durations: list[float] | None = None,
) -> ManifestSet:
    """Build every canonical manifest and stage it under ``out_dir``.

    Returns a :class:`ManifestSet` mapping relative S3 keys (no leading
    slash) to ready-to-upload bytes. The same bytes are written to
    ``out_dir/<relative key>`` atomically (temp file + rename) so the worker
    can upload straight from local staging.
    """
    files: dict[str, bytes] = {}
    per_quality = durations or {}
    files[_hls_master_key(job_id)] = _master_playlist(job_id, qualities, segment_counts, cfg, has_audio)
    for quality in qualities:
        count = segment_counts.get(quality, 0)
        files[_hls_media_key(job_id, quality)] = _media_playlist(
            job_id, quality, count, cfg, keys, durations=per_quality.get(quality)
        )
    if has_audio:
        audio_count = len(audio_durations) if audio_durations else max(segment_counts.values())
        files[_hls_audio_key(job_id)] = _audio_playlist(
            job_id, audio_count, cfg, keys, durations=audio_durations
        )
    files[_dash_key(job_id)] = build_dash_mpd(
        job_id,
        qualities,
        segment_counts,
        cfg,
        keys,
        has_audio,
        durations=per_quality,
        audio_durations=audio_durations,
    )

    for relative_key, payload in sorted(files.items()):
        _write_atomic(out_dir, relative_key, payload)
    log.info(
        "canonical manifests built",
        extra={"job_id": job_id, "qualities": qualities, "has_audio": has_audio},
    )
    return ManifestSet(files=files)


def manifest_json_bytes(document: dict) -> bytes:
    """Serialise a manifest document deterministically (sorted keys)."""
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
