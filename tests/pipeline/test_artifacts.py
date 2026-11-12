"""Tests for the canonical HLS/DASH manifests and manifest.json."""

from __future__ import annotations

import base64
import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from vg_pipeline.artifacts import (
    VARIANT_TOKEN,
    build_canonical_manifests,
    build_manifest_json,
    manifest_json_bytes,
)
from vg_pipeline.contracts import CencKeys, PipelineConfig
from vg_pipeline.errors import PipelineError

JOB = "0f0e0d0c-1111-4222-8333-444455556666"
KID = "aa" * 16
KEY = "bb" * 16
IV = "cc" * 16


def keys() -> CencKeys:
    """A CencKeys with a combined PSSH of one Widevine box (synthetic)."""
    payload = b"widevine-payload"
    size = 4 + 4 + 4 + 16 + 4 + len(payload)
    box = (
        struct.pack(">I", size)
        + b"pssh"
        + struct.pack(">I", 0)
        + bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
        + struct.pack(">I", len(payload))
        + payload
    )
    return CencKeys(
        key_id_hex=KID,
        key_hex=KEY,
        iv_hex=IV,
        pssh_hex=box.hex(),
        skd_uri="skd://vg-test-provider/x",
    )


@pytest.fixture()
def staged(tmp_path: Path, pipeline_cfg: PipelineConfig) -> tuple[dict[str, bytes], Path]:
    out_dir = tmp_path / "staging"
    manifest_set = build_canonical_manifests(
        job_id=JOB,
        out_dir=out_dir,
        qualities=["720p", "480p"],
        segment_counts={"720p": 3, "480p": 3},
        cfg=pipeline_cfg,
        keys=keys(),
        has_audio=True,
    )
    return manifest_set.files, out_dir


class TestManifestSetKeys:
    def test_relative_s3_keys(self, staged: tuple[dict[str, bytes], Path]) -> None:
        files, _ = staged
        root = f"renditions/{JOB}/hls"
        assert set(files) == {
            f"{root}/master.m3u8",
            f"{root}/media_720p.m3u8",
            f"{root}/media_480p.m3u8",
            f"{root}/audio.m3u8",
            f"renditions/{JOB}/dash/stream.mpd",
        }
        assert all(not key.startswith("/") for key in files)

    def test_files_staged_locally_atomically(self, staged: tuple[dict[str, bytes], Path]) -> None:
        files, out_dir = staged
        for key, payload in files.items():
            local = out_dir / key
            assert local.is_file()
            assert local.read_bytes() == payload
        # no temp files left behind
        assert not list(out_dir.rglob("*.tmp"))


class TestMediaPlaylist:
    def test_golden_shape(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        manifest_set = build_canonical_manifests(
            JOB, tmp_path, ["720p"], {"720p": 3}, pipeline_cfg, keys(), has_audio=False
        )
        text = manifest_set.files[f"renditions/{JOB}/hls/media_720p.m3u8"].decode("utf-8")
        lines = text.splitlines()
        assert lines[0] == "#EXTM3U"
        assert lines[1] == "#EXT-X-VERSION:7"
        assert lines[2] == "#EXT-X-TARGETDURATION:2"
        assert lines[3] == "#EXT-X-MEDIA-SEQUENCE:0"
        assert lines[4] == (
            f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{keys().skd_uri}",'
            'KEYFORMAT="com.apple.streamingkeydelivery",KEYFORMATVERSIONS="1"'
        )
        assert lines[5] == f'#EXT-X-MAP:URI="/{JOB}/init/720p/init.mp4"'
        # init URL carries NO variant token
        assert VARIANT_TOKEN not in lines[5]
        # 3 segments with the variant token and 5-digit numbering
        segment_lines = [line for line in lines if line.startswith("/")]
        assert segment_lines == [
            f"/{JOB}/{VARIANT_TOKEN}/720p/seg_{i:05d}.m4s" for i in range(1, 4)
        ]
        assert lines.count("#EXTINF:2.000000,") == 3
        assert lines[-1] == "#EXT-X-ENDLIST"
        # every segment URI carries the literal token for the edge to rewrite
        assert all(VARIANT_TOKEN in line for line in segment_lines)

    def test_audio_playlist_no_variant_token(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        manifest_set = build_canonical_manifests(
            JOB, tmp_path, ["720p"], {"720p": 2}, pipeline_cfg, keys(), has_audio=True
        )
        text = manifest_set.files[f"renditions/{JOB}/hls/audio.m3u8"].decode("utf-8")
        assert f'#EXT-X-MAP:URI="/{JOB}/audio/init.mp4"' in text
        assert f"/{JOB}/audio/seg_00001.m4s" in text
        assert VARIANT_TOKEN not in text

    def test_zero_segments_raises(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        with pytest.raises(PipelineError) as excinfo:
            build_canonical_manifests(
                JOB, tmp_path, ["720p"], {"720p": 0}, pipeline_cfg, keys(), has_audio=False
            )
        assert excinfo.value.code == "MANIFEST_FAILED"


class TestMasterPlaylist:
    def test_lists_only_produced_qualities_with_audio_group(
        self, staged: tuple[dict[str, bytes], Path]
    ) -> None:
        files, _ = staged
        text = files[f"renditions/{JOB}/hls/master.m3u8"].decode("utf-8")
        assert "#EXTM3U" in text
        assert "#EXT-X-VERSION:7" in text
        assert f"/{JOB}/hls/media_720p.m3u8" in text
        assert f"/{JOB}/hls/media_480p.m3u8" in text
        assert "360p" not in text
        assert "240p" not in text
        assert 'TYPE=AUDIO,GROUP-ID="audio"' in text
        assert f'URI="/{JOB}/hls/audio.m3u8"' in text
        assert 'AUDIO="audio"' in text
        assert "RESOLUTION=1280x720" in text
        assert "BANDWIDTH=3800000" in text
        assert "AVERAGE-BANDWIDTH=2800000" in text

    def test_no_audio_group_without_audio(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        manifest_set = build_canonical_manifests(
            JOB, tmp_path, ["720p"], {"720p": 1}, pipeline_cfg, keys(), has_audio=False
        )
        text = manifest_set.files[f"renditions/{JOB}/hls/master.m3u8"].decode("utf-8")
        assert "EXT-X-MEDIA" not in text
        assert 'AUDIO="audio"' not in text
        assert f"renditions/{JOB}/hls/audio.m3u8" not in manifest_set.files

    def test_unknown_quality_raises(self, pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
        with pytest.raises(PipelineError) as excinfo:
            build_canonical_manifests(
                JOB, tmp_path, ["144p"], {"144p": 2}, pipeline_cfg, keys(), has_audio=False
            )
        assert excinfo.value.code == "MANIFEST_FAILED"


class TestDashMpd:
    def test_valid_xml_with_expected_structure(self, staged: tuple[dict[str, bytes], Path]) -> None:
        files, _ = staged
        mpd_bytes = files[f"renditions/{JOB}/dash/stream.mpd"]
        root = ET.fromstring(mpd_bytes)
        assert root.tag == "{urn:mpeg:dash:schema:mpd:2011}MPD"
        assert root.get("type") == "static"
        assert root.get("mediaPresentationDuration") == "PT6S"
        ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
        adaptations = root.findall(".//d:AdaptationSet", ns)
        assert len(adaptations) == 3  # 720p, 480p, audio

        templates = root.findall(".//d:SegmentTemplate", ns)
        assert len(templates) == 3
        video_templates = [
            t for t in templates if VARIANT_TOKEN in (t.get("media") or "")
        ]
        assert len(video_templates) == 2
        for template in video_templates:
            assert template.get("initialization") == f"/{JOB}/init/720p/init.mp4" or template.get(
                "initialization"
            ) == f"/{JOB}/init/480p/init.mp4"
            assert VARIANT_TOKEN not in template.get("initialization", "")
            media = template.get("media", "")
            assert VARIANT_TOKEN in media
            assert "$Number%05d$" in media
            assert template.get("startNumber") == "1"
            assert template.get("duration") == "2"
            assert template.get("timescale") == "1"

    def test_content_protection_carries_pssh(self, staged: tuple[dict[str, bytes], Path]) -> None:
        files, _ = staged
        root = ET.fromstring(files[f"renditions/{JOB}/dash/stream.mpd"])
        ns = {"d": "urn:mpeg:dash:schema:mpd:2011", "cenc": "urn:mpeg:cenc:2013"}
        protections = root.findall(".//d:ContentProtection", ns)
        scheme_markers = [p for p in protections if p.get("schemeIdUri") == "urn:mpeg:dash:mp4:protection:2011"]
        assert len(scheme_markers) >= 1
        assert scheme_markers[0].get("value") == "cbcS"
        pssh_elements = root.findall(".//cenc:pssh", ns)
        assert len(pssh_elements) >= 1
        # the cenc:pssh text is base64 of the combined box bytes
        decoded = base64.b64decode(pssh_elements[0].text or "")
        assert bytes.fromhex(keys().pssh_hex) == decoded

    def test_audio_adaptation_has_no_variant_token(self, staged: tuple[dict[str, bytes], Path]) -> None:
        files, _ = staged
        text = files[f"renditions/{JOB}/dash/stream.mpd"].decode("utf-8")
        assert f"/{JOB}/audio/seg_$Number%05d$.m4s" in text
        assert f'initialization="/{JOB}/audio/init.mp4"' in text


class TestManifestJson:
    def test_schema_fields_and_no_secrets(self, pipeline_cfg: PipelineConfig) -> None:
        document = build_manifest_json(
            job_id=JOB,
            qualities=["720p", "480p"],
            segment_counts={"720p": 3, "480p": 3},
            cfg=pipeline_cfg,
            keys=keys(),
            has_audio=True,
            retention="ttl30",
            source_sha256="deadbeef",
            artifact_sha256={"renditions/x/a.mp4": "cafe"},
            generator_git_sha="abc123",
            generator_image_digest="sha256:7c037f",
            packager_version="2.6.1",
            ffmpeg_version="n7.1.5",
            ami_id="ami-0123",
            watermark_secret_version="v1",
            completed_at="2026-01-01T00:00:00Z",
        )
        text = manifest_json_bytes(document).decode("utf-8")
        document = json.loads(text)
        assert document["schema_version"] == 1
        assert document["job_id"] == JOB
        assert document["profile"] == "hevc-v1"
        assert [r["name"] for r in document["ladder"]] == ["720p", "480p"]
        assert document["segment_counts"] == {"720p": 3, "480p": 3}
        assert document["key_id"] == KID
        assert document["source_sha256"] == "deadbeef"
        assert document["retention"] == "ttl30"
        assert document["generator_image_digest"] == "sha256:7c037f"
        assert document["watermark_secret_version"] == "v1"
        # NEVER the content key or IV
        assert KEY not in text
        assert IV not in text
        assert "key_hex" not in text
        assert "iv_hex" not in text
        assert '"key"' not in text.replace('"key_id"', "")

    def test_ladder_only_produced_qualities(self, pipeline_cfg: PipelineConfig) -> None:
        document = build_manifest_json(
            JOB, ["480p"], {"480p": 2}, pipeline_cfg, keys(), has_audio=False
        )
        assert [entry["name"] for entry in document["ladder"]] == ["480p"]
        assert document["qualities"] == ["480p"]


def test_media_playlist_uses_real_per_segment_durations(pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
    manifest_set = build_canonical_manifests(
        JOB,
        tmp_path,
        ["720p"],
        {"720p": 3},
        pipeline_cfg,
        keys(),
        has_audio=False,
        durations={"720p": [2.0, 2.0, 1.25]},
    )
    text = manifest_set.files[f"renditions/{JOB}/hls/media_720p.m3u8"].decode("utf-8")
    assert "#EXT-X-TARGETDURATION:2" in text
    assert "#EXTINF:1.250000," in text


def test_dash_uses_segment_timeline_when_durations_exist(pipeline_cfg: PipelineConfig, tmp_path: Path) -> None:
    manifest_set = build_canonical_manifests(
        JOB,
        tmp_path,
        ["720p"],
        {"720p": 3},
        pipeline_cfg,
        keys(),
        has_audio=False,
        durations={"720p": [2.0, 2.0, 1.0]},
    )
    root = ET.fromstring(manifest_set.files[f"renditions/{JOB}/dash/stream.mpd"])
    assert root.get("mediaPresentationDuration") == "PT5S"
    ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
    timeline = root.find(".//d:SegmentTimeline", ns)
    assert timeline is not None
    assert [s.get("d") for s in timeline.findall("d:S", ns)] == ["2000", "2000", "1000"]
